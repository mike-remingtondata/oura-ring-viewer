"""Tests for the staff chart API, against real tables and real templates."""

import datetime as dt
import json
from http import HTTPStatus
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse
from canvas_sdk.test_utils.factories import PatientFactory
from canvas_sdk.v1.data.common import ContactPointState, ContactPointSystem, ContactPointUse
from canvas_sdk.v1.data.patient import PatientContactPoint

from oura_ring_viewer.models import OuraDaily, OuraOAuthToken
from oura_ring_viewer.protocols.chart_api import OuraChartAPI, _has_messageable_channel
from oura_ring_viewer.services import magic_link
from oura_ring_viewer.services.oauth import RefreshFailed, TokensNotFound
from oura_ring_viewer.services.oura_client import OuraAPIError, OuraAuthError
from oura_ring_viewer.services.storage import get_sync_state, upsert_tokens
from oura_ring_viewer.services.sync import SyncResult

PATIENT = "abc123"
SECRETS = {
    "OURA_CLIENT_ID": "cid",
    "OURA_CLIENT_SECRET": "sec",
    "OURA_REDIRECT_URI": "https://acme.canvasmedical.com/plugin-io/api/oura_ring_viewer/callback",
    "OURA_ENVIRONMENT": "sandbox",
    "OURA_MAGIC_LINK_SECRET": "m" * 40,
}
STAFF = "staff-42"


class _Request:
    def __init__(self, query: dict[str, str], headers: dict[str, str]) -> None:
        self.query_params = query
        self.headers = headers


class _Event:
    def __init__(self, method: str, path: str) -> None:
        self.context = {"method": method, "path": path}


def _api(
    method: str,
    path: str,
    query: dict[str, str] | None = None,
    *,
    secrets: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> OuraChartAPI:
    handler = OuraChartAPI(event=_Event(method, path))
    handler.request = _Request({"patient_id": PATIENT, **(query or {})}, headers or {})
    handler.secrets = SECRETS if secrets is None else secrets
    return handler


def _json(effect: Any) -> Any:
    assert isinstance(effect, JSONResponse)
    return json.loads(effect.content)


def _connect() -> None:
    upsert_tokens(
        PATIENT,
        access_token_ciphertext="AT",
        refresh_token_ciphertext="RT",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
        scopes="daily spo2",
        now=dt.datetime.now(dt.timezone.utc),
        is_initial_connection=True,
    )


ROUTES = [
    ("GET", "/", "chart_shell"),
    ("GET", "/data", "chart_data"),
    ("POST", "/sync", "sync"),
    ("POST", "/send-link", "send_link"),
    ("POST", "/disconnect", "disconnect"),
]


@pytest.mark.parametrize("method, path, name", ROUTES)
@pytest.mark.parametrize("patient_id", ["", "bad id", '"><script>alert(1)</script>'])
def test_every_route_rejects_an_implausible_patient_id(
    method: str, path: str, name: str, patient_id: str
) -> None:
    handler = _api(method, path, {"patient_id": patient_id})
    effects = getattr(handler, name)()
    assert effects[0].status_code == HTTPStatus.BAD_REQUEST
    assert _json(effects[0]) == {"error": "invalid_patient_id"}


class TestShellAndData:
    def test_shell_renders_the_real_template_bound_to_the_patient(self) -> None:
        effect = _api("GET", "/").chart_shell()[0]
        assert isinstance(effect, HTMLResponse)
        html = effect.content.decode()
        assert 'var PATIENT_ID = "abc123";' in html
        assert 'var API = "/plugin-io/api/oura_ring_viewer";' in html
        assert "<script src=" not in html, "the chart must not load external scripts"

    def test_data_returns_the_view_model(self) -> None:
        body = _json(_api("GET", "/data", {"range": "30d"}).chart_data()[0])
        assert body["connection_status"] == "disconnected"
        assert body["range"] == "30d"
        assert len(body["days"]) == 30

    def test_unknown_range_falls_back_to_the_default(self) -> None:
        body = _json(_api("GET", "/data", {"range": "999"}).chart_data()[0])
        assert body["range"] == "14d"


class TestSync:
    def test_reports_missing_settings_without_calling_oura(self) -> None:
        with patch("oura_ring_viewer.protocols.chart_api.sync_patient") as sync:
            effect = _api("POST", "/sync", secrets={"OURA_ENVIRONMENT": "sandbox"}).sync()[0]
        sync.assert_not_called()
        assert effect.status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert set(_json(effect)["settings"]) == {
            "OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "OURA_REDIRECT_URI"
        }

    def test_success(self) -> None:
        result = SyncResult(4, 1, dt.date(2026, 11, 1), ("daily_spo2",))
        with patch("oura_ring_viewer.protocols.chart_api.sync_patient", return_value=result):
            body = _json(_api("POST", "/sync", {"range": "7d"}).sync()[0])
        assert body == {
            "rows_written": 4,
            "rows_purged": 1,
            "latest_day": "2026-11-01",
            "forbidden_endpoints": ["daily_spo2"],
        }

    @pytest.mark.parametrize(
        "error, status, code",
        [
            (TokensNotFound("p"), HTTPStatus.CONFLICT, "not_connected"),
            (RefreshFailed("x"), HTTPStatus.BAD_GATEWAY, "reconnect_required"),
            (OuraAuthError("x"), HTTPStatus.BAD_GATEWAY, "reconnect_required"),
            (OuraAPIError(429, "slow down"), HTTPStatus.BAD_GATEWAY, "rate_limited"),
            (OuraAPIError(500, "down"), HTTPStatus.BAD_GATEWAY, "oura_unavailable"),
        ],
    )
    def test_failures_map_to_staff_facing_codes(
        self, error: Exception, status: HTTPStatus, code: str
    ) -> None:
        with patch("oura_ring_viewer.protocols.chart_api.sync_patient", side_effect=error):
            effect = _api("POST", "/sync").sync()[0]
        assert effect.status_code == status
        assert _json(effect) == {"error": code}

    def test_a_programming_error_is_not_swallowed(self) -> None:
        with patch("oura_ring_viewer.protocols.chart_api.sync_patient", side_effect=KeyError("x")):
            with pytest.raises(KeyError):
                _api("POST", "/sync").sync()


class TestSendLink:
    def _send(self, channels: str | None = "", **kwargs: Any) -> list[Any]:
        query = {} if channels is None else {"channels": channels}
        return list(_api("POST", "/send-link", query, **kwargs).send_link())

    def test_copy_only_link_is_signed_stored_and_returned(self) -> None:
        body = _json(self._send()[-1])
        prefix = "https://acme.canvasmedical.com/plugin-io/api/oura_ring_viewer/connect?token="
        assert body["connection_link"].startswith(prefix)
        assert body["expires_in_seconds"] == 900
        assert (body["portal_status"], body["portal_error"]) == ("skipped", "not_selected")

        token = body["connection_link"][len(prefix):]
        claims = magic_link.verify_link(token, SECRETS["OURA_MAGIC_LINK_SECRET"])
        state = get_sync_state(PATIENT)
        assert state is not None
        assert claims.patient_id == PATIENT
        assert state.last_link_nonce == claims.nonce
        assert state.link_pending is True

    def test_a_new_link_replaces_the_previous_nonce(self) -> None:
        self._send()
        first = get_sync_state(PATIENT)
        self._send()
        second = get_sync_state(PATIENT)
        assert first is not None and second is not None
        assert first.last_link_nonce != second.last_link_nonce

    @pytest.mark.parametrize(
        "override, expected",
        [
            ({"OURA_MAGIC_LINK_SECRET": "short"}, "OURA_MAGIC_LINK_SECRET (shorter than 32 characters)"),
            ({"OURA_REDIRECT_URI": "http://acme.example/cb"}, "OURA_REDIRECT_URI (must be an absolute https URL)"),
        ],
    )
    def test_unsafe_configuration_issues_no_link(self, override: dict[str, str], expected: str) -> None:
        effects = self._send(secrets={**SECRETS, **override})
        assert len(effects) == 1
        assert effects[0].status_code == HTTPStatus.SERVICE_UNAVAILABLE
        assert _json(effects[0])["settings"] == [expected]
        assert get_sync_state(PATIENT) is None

    def test_portal_is_the_default_and_needs_a_staff_sender(self) -> None:
        body = _json(self._send(channels=None)[-1])
        assert (body["portal_status"], body["portal_error"]) == ("skipped", "no_staff_sender")

    def test_portal_is_skipped_when_the_patient_cannot_be_messaged(self) -> None:
        body = _json(self._send(channels="portal", headers={"canvas-logged-in-user-id": STAFF})[-1])
        assert (body["portal_status"], body["portal_error"]) == ("skipped", "no_messageable_channel")

    def test_portal_message_effect_is_emitted_before_the_response(self) -> None:
        sentinel = MagicMock(name="message_effect")
        with (
            patch("oura_ring_viewer.protocols.chart_api._has_messageable_channel", return_value=True),
            patch("oura_ring_viewer.protocols.chart_api.Message") as message,
        ):
            message.return_value.create_and_send.return_value = sentinel
            effects = self._send(channels="portal", headers={"canvas-logged-in-user-id": STAFF})

        assert effects[0] is sentinel
        kwargs = message.call_args.kwargs
        assert (kwargs["sender_id"], kwargs["recipient_id"]) == (STAFF, PATIENT)
        assert _json(effects[-1])["connection_link"] in kwargs["content"]
        assert "\u2014" not in kwargs["content"]  # no em dashes in patient-facing copy
        assert _json(effects[-1])["portal_status"] == "sent"

    def test_a_rejected_portal_message_still_returns_the_link(self) -> None:
        with (
            patch("oura_ring_viewer.protocols.chart_api._has_messageable_channel", return_value=True),
            patch("oura_ring_viewer.protocols.chart_api.Message") as message,
        ):
            message.return_value.create_and_send.side_effect = ValidationError.from_exception_data(
                "Message", []
            )
            effects = self._send(channels="portal", headers={"canvas-logged-in-user-id": STAFF})
        body = _json(effects[-1])
        assert len(effects) == 1
        assert (body["portal_status"], body["portal_error"]) == ("failed", "invalid_sender_or_recipient")
        assert body["connection_link"]


class TestMessageableChannel:
    """Runs the contact-point query against real Patient rows, so the field
    names and enum values it depends on are checked against the SDK schema."""

    def _patient_with(self, **contact: Any) -> str:
        patient = PatientFactory.create()
        fields: dict[str, Any] = {
            "system": ContactPointSystem.EMAIL.value,
            "value": "pat@example.com",
            "use": ContactPointUse.HOME.value,
            "use_notes": "",
            "rank": 1,
            "state": ContactPointState.ACTIVE.value,
            "has_consent": True,
            "opted_out": False,
            "verification_token": "",
        }
        fields.update(contact)
        PatientContactPoint.objects.create(patient=patient, **fields)
        return str(patient.id)

    def test_consenting_active_email(self) -> None:
        assert _has_messageable_channel(self._patient_with()) is True

    def test_consenting_active_phone(self) -> None:
        patient_id = self._patient_with(system=ContactPointSystem.PHONE.value, value="5555550100")
        assert _has_messageable_channel(patient_id) is True

    @pytest.mark.parametrize(
        "contact",
        [
            {"has_consent": False},
            {"opted_out": True},
            {"state": ContactPointState.DELETED.value},
            {"system": ContactPointSystem.FAX.value},
        ],
    )
    def test_unusable_contact_points(self, contact: dict[str, Any]) -> None:
        assert _has_messageable_channel(self._patient_with(**contact)) is False

    def test_unknown_patient(self) -> None:
        assert _has_messageable_channel("no-such-patient") is False


def test_disconnect_removes_tokens_and_cached_data() -> None:
    _connect()
    OuraDaily.objects.create(
        patient_id=PATIENT, day=dt.date(2026, 11, 1), family="sleep",
        payload={"score": 80}, fetched_at=dt.datetime.now(dt.timezone.utc),
    )
    body = _json(_api("POST", "/disconnect").disconnect()[0])
    assert body == {"disconnected": True}
    assert not OuraOAuthToken.objects.filter(patient_id=PATIENT).exists()
    assert not OuraDaily.objects.filter(patient_id=PATIENT).exists()
