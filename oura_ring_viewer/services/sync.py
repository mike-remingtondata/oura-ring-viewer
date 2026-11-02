"""On-demand sync: pull Oura summaries, normalize, persist, purge.

One sync makes one call per endpoint (plus pagination, which a 90 day window
of daily summaries does not need). Tokens are refreshed at most once per sync:
after a rotation, every later endpoint reuses the new access token.

A 403 on an endpoint skips that family rather than failing the sync. See
``OuraForbiddenError`` for why a patient can legitimately be missing a family.
If every endpoint is forbidden, nothing useful was fetched, and that is
recorded as an error.
"""

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from oura_ring_viewer.services.crypto import TokenCipher
from oura_ring_viewer.services.normalize import (
    NormalizedDay,
    normalize_activity,
    normalize_readiness,
    normalize_sleep,
    normalize_spo2,
)
from oura_ring_viewer.services.oauth import (
    LoadedTokens,
    RefreshFailed,
    TokensNotFound,
    load_tokens,
    refresh_and_persist,
)
from oura_ring_viewer.services.oura_client import (
    OuraAPIError,
    OuraAuthError,
    OuraClient,
    OuraForbiddenError,
)
from oura_ring_viewer.services.settings import (
    ENDPOINT_DAILY_ACTIVITY,
    ENDPOINT_DAILY_READINESS,
    ENDPOINT_DAILY_SLEEP,
    ENDPOINT_DAILY_SPO2,
    ENDPOINT_SLEEP_PERIODS,
)
from oura_ring_viewer.services.storage import purge_old_daily, store_daily, upsert_sync_state

# Short machine-readable codes written to ``OuraSyncState.last_error``. The
# chart maps these to staff-facing wording; they never carry response bodies.
ERROR_NOT_CONNECTED = "not_connected"
ERROR_REFRESH_FAILED = "refresh_failed"
ERROR_AUTH_FAILED = "auth_failed"
ERROR_ALL_FORBIDDEN = "all_forbidden"
ERROR_API = "api_error"

_ENDPOINTS: tuple[str, ...] = (
    ENDPOINT_DAILY_SLEEP,
    ENDPOINT_SLEEP_PERIODS,
    ENDPOINT_DAILY_READINESS,
    ENDPOINT_DAILY_ACTIVITY,
    ENDPOINT_DAILY_SPO2,
)


@dataclass
class SyncResult:
    """Outcome of a single sync."""

    rows_written: int
    rows_purged: int
    latest_day: dt.date | None
    forbidden_endpoints: tuple[str, ...] = field(default_factory=tuple)


def sync_patient(
    *,
    patient_id: str,
    range_days: int,
    client: OuraClient,
    cipher: TokenCipher,
    now: dt.datetime,
) -> SyncResult:
    """Run a manual "Sync now" for one patient over the last ``range_days``.

    On failure the sync-state row records a short error code and the original
    exception is re-raised for the API handler to turn into a response.
    """
    try:
        tokens = load_tokens(patient_id, cipher)
    except TokensNotFound:
        _record_error(patient_id, ERROR_NOT_CONNECTED, now)
        raise

    today = now.date()
    start_day = today - dt.timedelta(days=range_days)

    fetched: dict[str, list[dict[str, Any]]] = {}
    forbidden: list[str] = []
    try:
        for endpoint in _ENDPOINTS:
            try:
                records, tokens = _fetch_with_refresh(
                    patient_id=patient_id,
                    client=client,
                    cipher=cipher,
                    tokens=tokens,
                    endpoint=endpoint,
                    start_day=start_day,
                    end_day=today,
                    now=now,
                )
            except OuraForbiddenError:
                forbidden.append(endpoint)
                continue
            fetched[endpoint] = records
    except RefreshFailed:
        _record_error(patient_id, ERROR_REFRESH_FAILED, now)
        raise
    except OuraAuthError:
        # Still rejected after a successful refresh.
        _record_error(patient_id, ERROR_AUTH_FAILED, now)
        raise
    except OuraAPIError:
        _record_error(patient_id, ERROR_API, now)
        raise

    if not fetched:
        _record_error(patient_id, ERROR_ALL_FORBIDDEN, now)
        return SyncResult(
            rows_written=0,
            rows_purged=0,
            latest_day=None,
            forbidden_endpoints=tuple(forbidden),
        )

    rows = _in_window(_normalize_all(fetched), start_day, today)
    written = store_daily(patient_id, rows, now=now)
    purged = purge_old_daily(patient_id, today=today)
    latest_day = max((row.day for row in rows), default=None)

    state: dict[str, Any] = {"last_synced_at": now, "last_error": "", "last_error_at": None}
    if latest_day is not None:
        state["last_day_synced"] = latest_day
    upsert_sync_state(patient_id, **state)

    return SyncResult(
        rows_written=written,
        rows_purged=purged,
        latest_day=latest_day,
        forbidden_endpoints=tuple(forbidden),
    )


def _normalize_all(fetched: dict[str, list[dict[str, Any]]]) -> list[NormalizedDay]:
    """Normalize whatever was fetched; a missing endpoint contributes nothing."""
    rows: list[NormalizedDay] = []
    if ENDPOINT_DAILY_SLEEP in fetched or ENDPOINT_SLEEP_PERIODS in fetched:
        rows += normalize_sleep(
            fetched.get(ENDPOINT_DAILY_SLEEP, []), fetched.get(ENDPOINT_SLEEP_PERIODS, [])
        )
    rows += normalize_readiness(fetched.get(ENDPOINT_DAILY_READINESS, []))
    rows += normalize_activity(fetched.get(ENDPOINT_DAILY_ACTIVITY, []))
    rows += normalize_spo2(fetched.get(ENDPOINT_DAILY_SPO2, []))
    return rows


def _in_window(
    rows: list[NormalizedDay], start_day: dt.date, end_day: dt.date
) -> list[NormalizedDay]:
    """Drop rows outside the requested window.

    The client widens ``end_date`` by a day to be safe about Oura's bound, and
    a ring owner ahead of UTC can already have a "tomorrow" summary, so rows
    are filtered here rather than trusted.
    """
    return [row for row in rows if start_day <= row.day <= end_day]


def _fetch_with_refresh(
    *,
    patient_id: str,
    client: OuraClient,
    cipher: TokenCipher,
    tokens: LoadedTokens,
    endpoint: str,
    start_day: dt.date,
    end_day: dt.date,
    now: dt.datetime,
) -> tuple[list[dict[str, Any]], LoadedTokens]:
    """Fetch one endpoint, refreshing once on 401.

    Returns the records and the possibly-rotated tokens so later endpoints in
    the same sync reuse the new access token instead of refreshing again.
    """
    try:
        return client.fetch_collection(tokens.access_token, endpoint, start_day, end_day), tokens
    except OuraAuthError:
        rotated = refresh_and_persist(patient_id, client, cipher, tokens.refresh_token, now=now)
        records = client.fetch_collection(rotated.access_token, endpoint, start_day, end_day)
        return records, rotated


def _record_error(patient_id: str, code: str, now: dt.datetime) -> None:
    upsert_sync_state(patient_id, last_error=code, last_error_at=now)
