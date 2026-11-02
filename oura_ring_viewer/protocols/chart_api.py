"""Staff-authenticated SimpleAPI for the chart-drawer Application.

Routes:
  GET  /                       -> HTML shell (inline CSS and JS, no external scripts)
  GET  /data?patient_id=&range -> JSON view-model, served from local tables only
  POST /sync?patient_id=&range -> manual pull from Oura
  POST /send-link?patient_id=  -> mint a connection link, optionally via the portal
  POST /disconnect?patient_id= -> purge tokens and every cached row

Every route requires a Canvas staff session (``StaffSessionAuthMixin``) and
rejects a ``patient_id`` that is not a plausible Canvas key before touching the
database or a template. Patient visibility follows Canvas's staff model: like
``dexcom-cgm-viewer``, this plugin does not add a per-patient authorization
check of its own. No route ever returns token material.
"""

import datetime as dt
from http import HTTPStatus

from pydantic import ValidationError

from canvas_sdk.effects import Effect
from canvas_sdk.effects.note.message import Message
from canvas_sdk.effects.simple_api import HTMLResponse, JSONResponse, Response
from canvas_sdk.handlers.simple_api import SimpleAPI, StaffSessionAuthMixin, api
from canvas_sdk.templates import render_to_string
from canvas_sdk.v1.data import Patient
from canvas_sdk.v1.data.common import ContactPointState, ContactPointSystem
from logger import log

from oura_ring_viewer.services import magic_link
from oura_ring_viewer.services.chart_data import build_payload
from oura_ring_viewer.services.config import (
    LINK_SECRETS,
    MAGIC_LINK_SECRET,
    REDIRECT_URI,
    SYNC_SECRETS,
    build_client,
    clean_patient_id,
    config_problems,
    connect_url,
)
from oura_ring_viewer.services.crypto import TokenCipher
from oura_ring_viewer.services.oauth import RefreshFailed, TokensNotFound
from oura_ring_viewer.services.oura_client import OuraAPIError, OuraAuthError
from oura_ring_viewer.services.settings import (
    MAGIC_LINK_TTL_SECONDS,
    PLUGIN_API_PATH,
    parse_range_days,
)
from oura_ring_viewer.services.storage import delete_all_for_patient, upsert_sync_state
from oura_ring_viewer.services.sync import sync_patient

STAFF_ID_HEADER = "canvas-logged-in-user-id"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class OuraChartAPI(StaffSessionAuthMixin, SimpleAPI):
    """Staff-facing chart API. All routes require a Canvas staff session."""

    @api.get("/")
    def chart_shell(self) -> list[Response | Effect]:
        """Serve the chart HTML shell bound to the active patient."""
        patient_id = clean_patient_id(self.request.query_params.get("patient_id"))
        if patient_id is None:
            return [_bad_patient()]
        html = render_to_string(
            "templates/chart.html",
            context={"patient_id": patient_id, "api_path": PLUGIN_API_PATH},
        )
        return [HTMLResponse(content=html or "", status_code=HTTPStatus.OK)]

    @api.get("/data")
    def chart_data(self) -> list[Response | Effect]:
        """Return the JSON view-model for the requested range."""
        patient_id = clean_patient_id(self.request.query_params.get("patient_id"))
        if patient_id is None:
            return [_bad_patient()]
        range_days = parse_range_days(self.request.query_params.get("range"))
        # Reads local tables only; an exception here is a bug and should
        # surface to monitoring rather than be swallowed.
        return [JSONResponse(content=build_payload(patient_id, range_days, now=_now()))]

    @api.post("/sync")
    def sync(self) -> list[Response | Effect]:
        """Pull fresh summaries from Oura for the requested range."""
        patient_id = clean_patient_id(self.request.query_params.get("patient_id"))
        if patient_id is None:
            return [_bad_patient()]
        problems = config_problems(self.secrets, SYNC_SECRETS)
        if problems:
            return [_misconfigured(problems)]

        try:
            result = sync_patient(
                patient_id=patient_id,
                range_days=parse_range_days(self.request.query_params.get("range")),
                client=build_client(self.secrets),
                cipher=TokenCipher(),
                now=_now(),
            )
        except TokensNotFound:
            return [_error("not_connected", HTTPStatus.CONFLICT)]
        except (RefreshFailed, OuraAuthError) as exc:
            # sync_patient has already recorded the error code the chart uses
            # to offer a reconnect. Do not overwrite it here.
            log.warning(f"oura sync for {patient_id} needs reconnect: {type(exc).__name__}")
            return [_error("reconnect_required", HTTPStatus.BAD_GATEWAY)]
        except OuraAPIError as exc:
            log.warning(f"oura sync for {patient_id} failed with HTTP {exc.status_code}")
            code = "rate_limited" if exc.status_code == HTTPStatus.TOO_MANY_REQUESTS else "oura_unavailable"
            return [_error(code, HTTPStatus.BAD_GATEWAY)]

        return [JSONResponse(content={
            "rows_written": result.rows_written,
            "rows_purged": result.rows_purged,
            "latest_day": result.latest_day.isoformat() if result.latest_day else None,
            "forbidden_endpoints": list(result.forbidden_endpoints),
        })]

    @api.post("/send-link")
    def send_link(self) -> list[Response | Effect]:
        """Mint a connection link and optionally deliver it via the patient portal.

        ``channels`` is ``portal`` (the default) or empty for copy-link only.
        The link is always returned so staff can share it another way if
        portal delivery is unavailable.
        """
        patient_id = clean_patient_id(self.request.query_params.get("patient_id"))
        if patient_id is None:
            return [_bad_patient()]
        problems = config_problems(self.secrets, LINK_SECRETS)
        if problems:
            return [_misconfigured(problems)]

        now = _now()
        nonce = magic_link.new_nonce()
        token = magic_link.mint_link(
            patient_id, nonce, self.secrets[MAGIC_LINK_SECRET], now=int(now.timestamp())
        )
        link = connect_url(self.secrets[REDIRECT_URI], token)
        # Storing the nonce invalidates any earlier link for this patient.
        upsert_sync_state(patient_id, last_link_sent_at=now, last_link_nonce=nonce, link_pending=True)

        effects: list[Response | Effect] = []
        portal_status = "skipped"
        portal_error: str | None = "not_selected"
        if "portal" in _channels(self.request.query_params.get("channels")):
            effect, portal_status, portal_error = _portal_message(
                sender_id=(self.request.headers.get(STAFF_ID_HEADER) or "").strip(),
                patient_id=patient_id,
                link=link,
            )
            if effect is not None:
                effects.append(effect)

        effects.append(JSONResponse(content={
            "connection_link": link,
            "link_sent_at": now.isoformat(),
            "expires_in_seconds": MAGIC_LINK_TTL_SECONDS,
            "portal_status": portal_status,
            "portal_error": portal_error,
        }))
        return effects

    @api.post("/disconnect")
    def disconnect(self) -> list[Response | Effect]:
        """Purge stored tokens and every cached row for the patient."""
        patient_id = clean_patient_id(self.request.query_params.get("patient_id"))
        if patient_id is None:
            return [_bad_patient()]
        delete_all_for_patient(patient_id)
        return [JSONResponse(content={"disconnected": True})]


def _channels(raw: str | None) -> set[str]:
    """Absent means portal; an explicit empty value means copy-link only."""
    if raw is None:
        return {"portal"}
    return {part.strip().lower() for part in raw.split(",") if part.strip()} & {"portal"}


def _portal_message(
    *, sender_id: str, patient_id: str, link: str
) -> tuple[Effect | None, str, str | None]:
    """Build the portal ``Message`` effect, or explain why it was skipped.

    Canvas fails a message with "Channel not supported" when the patient has no
    consenting, active contact point, so that is checked first rather than
    emitting an effect known to fail.
    """
    if not sender_id:
        return None, "skipped", "no_staff_sender"
    if not _has_messageable_channel(patient_id):
        return None, "skipped", "no_messageable_channel"
    body = (
        "Your care team has shared a secure link to connect your Oura Ring to your "
        "health record. Open it on your phone within 15 minutes:\n\n"
        f"{link}\n\n"
        "Your care team will see your sleep, readiness, activity and blood oxygen "
        "summaries. If you did not expect this message, you can ignore it."
    )
    try:
        effect = Message(content=body, sender_id=sender_id, recipient_id=patient_id).create_and_send()
    except ValidationError as exc:
        log.warning(f"portal message for {patient_id} not built: {exc.error_count()} validation error(s)")
        return None, "failed", "invalid_sender_or_recipient"
    return effect, "sent", None


def _has_messageable_channel(patient_id: str) -> bool:
    patient = Patient.objects.filter(id=patient_id).first()
    if patient is None:
        return False
    return bool(
        patient.telecom.filter(
            system__in=(ContactPointSystem.EMAIL.value, ContactPointSystem.PHONE.value),
            has_consent=True,
            opted_out=False,
            state=ContactPointState.ACTIVE.value,
        ).exists()
    )


def _error(code: str, status: HTTPStatus) -> JSONResponse:
    return JSONResponse(content={"error": code}, status_code=status)


def _bad_patient() -> JSONResponse:
    return _error("invalid_patient_id", HTTPStatus.BAD_REQUEST)


def _misconfigured(problems: list[str]) -> JSONResponse:
    return JSONResponse(
        content={"error": "plugin_not_configured", "settings": problems},
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
    )
