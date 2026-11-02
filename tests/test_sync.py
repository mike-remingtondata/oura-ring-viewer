"""Tests for the sync engine against the plugin's real SQLite tables.

The Oura client is mocked; storage, OAuth rotation and normalization are real.
"""

import datetime as dt
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from oura_ring_viewer.models import OuraDaily, OuraOAuthToken
from oura_ring_viewer.services.crypto import TokenCipher
from oura_ring_viewer.services.oauth import RefreshFailed, TokensNotFound
from oura_ring_viewer.services.oura_client import (
    OuraAPIError,
    OuraAuthError,
    OuraClient,
    OuraForbiddenError,
    TokenSet,
)
from oura_ring_viewer.services.settings import (
    DAILY_RETENTION_DAYS,
    FAMILY_ACTIVITY,
    FAMILY_READINESS,
    FAMILY_SLEEP,
    FAMILY_SPO2,
)
from oura_ring_viewer.services.storage import (
    delete_all_for_patient,
    fetch_daily_window,
    get_sync_state,
    upsert_tokens,
)
from oura_ring_viewer.services.sync import (
    ERROR_ALL_FORBIDDEN,
    ERROR_API,
    ERROR_AUTH_FAILED,
    ERROR_NOT_CONNECTED,
    ERROR_REFRESH_FAILED,
    sync_patient,
)

PATIENT = "patient-1"
OTHER = "patient-2"
NOW = dt.datetime(2026, 11, 2, 15, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()
DAY = (TODAY - dt.timedelta(days=1)).isoformat()


def _fixtures(day: str = DAY, *, score: int = 80) -> dict[str, list[dict[str, Any]]]:
    return {
        "daily_sleep": [{"day": day, "score": score}],
        "sleep": [
            {
                "day": day,
                "type": "long_sleep",
                "total_sleep_duration": 25_000,
                "efficiency": 90,
                "average_hrv": 45,
                "lowest_heart_rate": 50,
                "average_heart_rate": 55.0,
            }
        ],
        "daily_readiness": [{"day": day, "score": 75, "temperature_deviation": 0.1}],
        "daily_activity": [{"day": day, "score": 70, "steps": 9_000, "active_calories": 400}],
        "daily_spo2": [
            {"day": day, "spo2_percentage": {"average": 97.0}, "breathing_disturbance_index": 2}
        ],
    }


def _client(
    fixtures: dict[str, list[dict[str, Any]]] | None = None,
    *,
    fetch: Callable[..., Any] | None = None,
) -> MagicMock:
    client = MagicMock(spec=OuraClient)
    data = _fixtures() if fixtures is None else fixtures

    def default_fetch(access_token: str, endpoint: str, start: dt.date, end: dt.date) -> Any:
        return data.get(endpoint, [])

    client.fetch_collection.side_effect = fetch or default_fetch
    return client


def _connect(patient_id: str = PATIENT, access: str = "AT", refresh: str = "RT") -> None:
    upsert_tokens(
        patient_id,
        access_token_ciphertext=access,
        refresh_token_ciphertext=refresh,
        expires_at=NOW + dt.timedelta(hours=1),
        scopes="daily spo2",
        now=NOW,
        is_initial_connection=True,
    )


def _sync(client: MagicMock, *, patient_id: str = PATIENT, range_days: int = 14) -> Any:
    return sync_patient(
        patient_id=patient_id, range_days=range_days, client=client, cipher=TokenCipher(), now=NOW
    )


def _families(patient_id: str = PATIENT) -> set[str]:
    return set(OuraDaily.objects.filter(patient_id=patient_id).values_list("family", flat=True))


class TestHappyPath:
    def test_writes_every_family_and_clears_errors(self) -> None:
        _connect()
        result = _sync(_client())

        assert result.rows_written == 4
        assert result.forbidden_endpoints == ()
        assert result.latest_day == TODAY - dt.timedelta(days=1)
        assert _families() == {FAMILY_SLEEP, FAMILY_READINESS, FAMILY_ACTIVITY, FAMILY_SPO2}

        sleep = OuraDaily.objects.get(patient_id=PATIENT, family=FAMILY_SLEEP)
        assert sleep.payload["score"] == 80
        assert sleep.payload["average_hrv_ms"] == 45

        state = get_sync_state(PATIENT)
        assert state is not None
        assert state.last_synced_at == NOW
        assert state.last_day_synced == TODAY - dt.timedelta(days=1)
        assert state.last_error == ""

    def test_requests_the_full_range_ending_today(self) -> None:
        _connect()
        client = _client()
        _sync(client, range_days=30)
        _, _, start, end = client.fetch_collection.call_args.args
        assert (start, end) == (TODAY - dt.timedelta(days=30), TODAY)

    def test_resync_is_idempotent_and_updates_values(self) -> None:
        _connect()
        _sync(_client(_fixtures(score=80)))
        _sync(_client(_fixtures(score=85)))
        assert OuraDaily.objects.filter(patient_id=PATIENT).count() == 4
        sleep = OuraDaily.objects.get(patient_id=PATIENT, family=FAMILY_SLEEP)
        assert sleep.payload["score"] == 85

    def test_rows_outside_the_window_are_dropped(self) -> None:
        _connect()
        too_old = (TODAY - dt.timedelta(days=60)).isoformat()
        tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
        fixtures = {
            "daily_readiness": [
                {"day": too_old, "score": 50},
                {"day": DAY, "score": 75},
                {"day": tomorrow, "score": 90},
            ]
        }
        _sync(_client(fixtures))
        days = list(OuraDaily.objects.filter(patient_id=PATIENT).values_list("day", flat=True))
        assert days == [TODAY - dt.timedelta(days=1)]

    def test_purges_rows_past_retention_without_touching_other_patients(self) -> None:
        _connect()
        ancient = TODAY - dt.timedelta(days=DAILY_RETENTION_DAYS + 5)
        for patient in (PATIENT, OTHER):
            OuraDaily.objects.create(
                patient_id=patient, day=ancient, family=FAMILY_SLEEP,
                payload={"score": 1}, fetched_at=NOW,
            )
        result = _sync(_client())
        assert result.rows_purged == 1
        assert not OuraDaily.objects.filter(patient_id=PATIENT, day=ancient).exists()
        assert OuraDaily.objects.filter(patient_id=OTHER, day=ancient).exists()


class TestTokenRotation:
    def test_refreshes_once_then_reuses_the_rotated_token(self) -> None:
        _connect(access="AT1", refresh="RT1")
        data = _fixtures()
        seen: list[str] = []

        def fetch(access_token: str, endpoint: str, start: dt.date, end: dt.date) -> Any:
            seen.append(access_token)
            if access_token == "AT1":
                raise OuraAuthError("expired")
            return data[endpoint]

        client = _client(fetch=fetch)
        client.refresh.return_value = TokenSet("AT2", "RT2", 86_400, "bearer", "daily spo2")
        result = _sync(client)

        client.refresh.assert_called_once_with("RT1")
        assert seen == ["AT1"] + ["AT2"] * 5
        assert result.rows_written == 4
        stored = OuraOAuthToken.objects.get(patient_id=PATIENT)
        assert (stored.access_token, stored.refresh_token) == ("AT2", "RT2")
        assert stored.last_refresh_at == NOW

    def test_failed_refresh_records_error_and_keeps_old_tokens(self) -> None:
        _connect(access="AT1", refresh="RT1")
        client = _client(fetch=lambda *a: (_ for _ in ()).throw(OuraAuthError("expired")))
        client.refresh.side_effect = OuraAuthError("invalid_grant")

        with pytest.raises(RefreshFailed):
            _sync(client)

        state = get_sync_state(PATIENT)
        assert state is not None and state.last_error == ERROR_REFRESH_FAILED
        assert OuraOAuthToken.objects.get(patient_id=PATIENT).refresh_token == "RT1"
        assert not OuraDaily.objects.filter(patient_id=PATIENT).exists()

    def test_still_rejected_after_refresh_is_an_auth_failure(self) -> None:
        _connect()
        client = _client(fetch=lambda *a: (_ for _ in ()).throw(OuraAuthError("nope")))
        client.refresh.return_value = TokenSet("AT2", "RT2", 86_400, "bearer")

        with pytest.raises(OuraAuthError):
            _sync(client)
        state = get_sync_state(PATIENT)
        assert state is not None and state.last_error == ERROR_AUTH_FAILED


class TestPartialAccess:
    def test_a_forbidden_family_is_skipped_not_fatal(self) -> None:
        _connect()
        data = _fixtures()

        def fetch(access_token: str, endpoint: str, start: dt.date, end: dt.date) -> Any:
            if endpoint == "daily_spo2":
                raise OuraForbiddenError(403, "scope not granted")
            return data[endpoint]

        client = _client(fetch=fetch)
        result = _sync(client)

        assert result.forbidden_endpoints == ("daily_spo2",)
        assert _families() == {FAMILY_SLEEP, FAMILY_READINESS, FAMILY_ACTIVITY}
        client.refresh.assert_not_called()
        state = get_sync_state(PATIENT)
        assert state is not None and state.last_error == ""

    def test_everything_forbidden_records_an_error_and_writes_nothing(self) -> None:
        _connect()
        client = _client(fetch=lambda *a: (_ for _ in ()).throw(OuraForbiddenError(403, "")))
        result = _sync(client)

        assert result.rows_written == 0
        assert len(result.forbidden_endpoints) == 5
        client.refresh.assert_not_called()
        state = get_sync_state(PATIENT)
        assert state is not None and state.last_error == ERROR_ALL_FORBIDDEN


class TestFailures:
    def test_never_connected(self) -> None:
        with pytest.raises(TokensNotFound):
            _sync(_client())
        state = get_sync_state(PATIENT)
        assert state is not None and state.last_error == ERROR_NOT_CONNECTED

    def test_server_error_records_api_error(self) -> None:
        _connect()
        client = _client(fetch=lambda *a: (_ for _ in ()).throw(OuraAPIError(500, "down")))
        with pytest.raises(OuraAPIError):
            _sync(client)
        state = get_sync_state(PATIENT)
        assert state is not None and state.last_error == ERROR_API
        assert state.last_error_at == NOW


class TestChartReads:
    def test_window_is_inclusive_ordered_and_patient_scoped(self) -> None:
        for offset in (0, 3, 6):
            for family in (FAMILY_SPO2, FAMILY_SLEEP):
                OuraDaily.objects.create(
                    patient_id=PATIENT, day=TODAY - dt.timedelta(days=offset),
                    family=family, payload={}, fetched_at=NOW,
                )
        OuraDaily.objects.create(
            patient_id=OTHER, day=TODAY, family=FAMILY_SLEEP, payload={}, fetched_at=NOW
        )

        rows = fetch_daily_window(
            PATIENT, start_day=TODAY - dt.timedelta(days=3), end_day=TODAY
        )

        assert [(r.day, r.family) for r in rows] == [
            (TODAY - dt.timedelta(days=3), FAMILY_SLEEP),
            (TODAY - dt.timedelta(days=3), FAMILY_SPO2),
            (TODAY, FAMILY_SLEEP),
            (TODAY, FAMILY_SPO2),
        ]


class TestDisconnect:
    def test_removes_every_row_for_the_patient_only(self) -> None:
        _connect(PATIENT)
        _connect(OTHER)
        _sync(_client(), patient_id=PATIENT)
        _sync(_client(), patient_id=OTHER)

        delete_all_for_patient(PATIENT)

        assert not OuraOAuthToken.objects.filter(patient_id=PATIENT).exists()
        assert get_sync_state(PATIENT) is None
        assert not OuraDaily.objects.filter(patient_id=PATIENT).exists()
        assert OuraOAuthToken.objects.filter(patient_id=OTHER).exists()
        assert OuraDaily.objects.filter(patient_id=OTHER).count() == 4
