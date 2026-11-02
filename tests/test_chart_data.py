"""Tests for the chart view-model, against the plugin's real SQLite tables."""

import datetime as dt
from typing import Any

import pytest

from oura_ring_viewer.models import OuraDaily
from oura_ring_viewer.services.chart_data import build_payload
from oura_ring_viewer.services.settings import (
    DISCLAIMER,
    FAMILY_ACTIVITY,
    FAMILY_SLEEP,
    FAMILY_SPO2,
)
from oura_ring_viewer.services.storage import upsert_sync_state, upsert_tokens

PATIENT = "chart-patient"
NOW = dt.datetime(2026, 11, 2, 18, 0, tzinfo=dt.timezone.utc)
TODAY = NOW.date()


def _day(offset: int) -> dt.date:
    return TODAY - dt.timedelta(days=offset)


def _row(offset: int, family: str, payload: dict[str, Any], patient: str = PATIENT) -> None:
    OuraDaily.objects.create(
        patient_id=patient, day=_day(offset), family=family, payload=payload, fetched_at=NOW
    )


def _connect() -> None:
    upsert_tokens(
        PATIENT,
        access_token_ciphertext="AT",
        refresh_token_ciphertext="RT",
        expires_at=NOW + dt.timedelta(hours=1),
        scopes="daily spo2",
        now=NOW,
        is_initial_connection=True,
    )


class TestStatus:
    def test_never_connected(self) -> None:
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["connection_status"] == "disconnected"
        assert payload["has_data"] is False
        assert payload["last_synced_at"] is None
        assert payload["disclaimer"] == DISCLAIMER

    def test_link_sent_but_not_completed(self) -> None:
        upsert_sync_state(PATIENT, link_pending=True, last_link_sent_at=NOW)
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["connection_status"] == "link_pending"
        assert payload["link_sent_at"] == NOW.isoformat()

    def test_connected(self) -> None:
        _connect()
        upsert_sync_state(PATIENT, last_synced_at=NOW)
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["connection_status"] == "connected"
        assert payload["last_synced_at"] == NOW.isoformat()

    @pytest.mark.parametrize("error", ["refresh_failed", "auth_failed"])
    def test_dead_tokens_mean_reconnect(self, error: str) -> None:
        _connect()
        upsert_sync_state(PATIENT, last_error=error)
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["connection_status"] == "expired"
        assert payload["last_error"] == error

    @pytest.mark.parametrize("error", ["api_error", "all_forbidden"])
    def test_other_errors_keep_the_connection(self, error: str) -> None:
        _connect()
        upsert_sync_state(PATIENT, last_error=error)
        assert build_payload(PATIENT, 7, now=NOW)["connection_status"] == "connected"


class TestSeries:
    def test_window_is_exactly_range_days_ending_today(self) -> None:
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["range"] == "7d"
        assert payload["days"][0] == _day(6).isoformat()
        assert payload["days"][-1] == TODAY.isoformat()
        assert len(payload["days"]) == 7
        assert all(len(values) == 7 for values in payload["series"].values())

    def test_values_land_on_their_day_and_gaps_stay_gaps(self) -> None:
        _row(6, FAMILY_SLEEP, {"score": 70, "lowest_heart_rate_bpm": 52})
        _row(1, FAMILY_SLEEP, {"score": 80, "lowest_heart_rate_bpm": 50})
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["series"]["sleep_score"] == [70, None, None, None, None, 80, None]
        assert payload["series"]["resting_hr_bpm"] == [52, None, None, None, None, 50, None]
        assert payload["has_data"] is True

    def test_latest_is_tracked_per_metric(self) -> None:
        _row(1, FAMILY_SLEEP, {"score": 80})
        _row(0, FAMILY_ACTIVITY, {"steps": 4_000})
        payload = build_payload(PATIENT, 7, now=NOW)
        assert payload["latest"]["sleep_score"] == {"value": 80, "day": _day(1).isoformat()}
        assert payload["latest"]["steps"] == {"value": 4_000, "day": TODAY.isoformat()}
        assert payload["latest"]["spo2_pct"] is None

    def test_averages_ignore_gaps_and_non_numbers(self) -> None:
        _row(2, FAMILY_SPO2, {"average_pct": 96.0})
        _row(1, FAMILY_SPO2, {"average_pct": 97.5})
        _row(0, FAMILY_SPO2, {"average_pct": "bad"})
        averages = build_payload(PATIENT, 7, now=NOW)["averages"]
        assert averages["spo2_pct"] == 96.8
        assert averages["hrv_ms"] is None

    def test_rows_outside_the_window_or_for_other_patients_are_excluded(self) -> None:
        _row(10, FAMILY_SLEEP, {"score": 1})
        _row(0, FAMILY_SLEEP, {"score": 99}, patient="someone-else")
        assert build_payload(PATIENT, 7, now=NOW)["has_data"] is False

    def test_unsupported_range_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError):
            build_payload(PATIENT, 5, now=NOW)
