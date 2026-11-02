"""Build the JSON view-model for ``GET /data`` from the local plugin tables.

Reads only plugin tables, never Oura, so opening the chart costs no API call.

Series are dense: one slot per calendar day in the window, ``None`` where Oura
had nothing. The chart draws a gap as a gap instead of silently joining the
days on either side of it.
"""

import datetime as dt
from typing import Any

from oura_ring_viewer.services.settings import (
    DISCLAIMER,
    FAMILY_ACTIVITY,
    FAMILY_READINESS,
    FAMILY_SLEEP,
    FAMILY_SPO2,
    RANGE_OPTIONS,
)
from oura_ring_viewer.services.storage import fetch_daily_window, get_sync_state, get_tokens
from oura_ring_viewer.services.sync import ERROR_AUTH_FAILED, ERROR_REFRESH_FAILED

STATUS_CONNECTED = "connected"
STATUS_EXPIRED = "expired"
STATUS_LINK_PENDING = "link_pending"
STATUS_DISCONNECTED = "disconnected"

# Errors that mean the stored tokens are dead and the patient must reconnect.
_RECONNECT_ERRORS = frozenset({ERROR_REFRESH_FAILED, ERROR_AUTH_FAILED})

# (chart key, family, payload key). The chart reads only these keys.
METRICS: tuple[tuple[str, str, str], ...] = (
    ("sleep_score", FAMILY_SLEEP, "score"),
    ("total_sleep_seconds", FAMILY_SLEEP, "total_sleep_seconds"),
    ("resting_hr_bpm", FAMILY_SLEEP, "lowest_heart_rate_bpm"),
    ("hrv_ms", FAMILY_SLEEP, "average_hrv_ms"),
    ("readiness_score", FAMILY_READINESS, "score"),
    ("steps", FAMILY_ACTIVITY, "steps"),
    ("spo2_pct", FAMILY_SPO2, "average_pct"),
)


def build_payload(patient_id: str, range_days: int, *, now: dt.datetime) -> dict[str, Any]:
    """Shape everything the chart needs for one patient and range."""
    if range_days not in RANGE_OPTIONS:
        raise ValueError(f"unsupported range_days: {range_days}")

    state = get_sync_state(patient_id)
    last_error = state.last_error if state else ""
    status = _status(
        has_tokens=get_tokens(patient_id) is not None,
        last_error=last_error,
        link_pending=bool(state.link_pending) if state else False,
    )

    end_day = now.date()
    start_day = end_day - dt.timedelta(days=range_days - 1)
    days = [start_day + dt.timedelta(days=i) for i in range(range_days)]
    index = {day: i for i, day in enumerate(days)}

    series: dict[str, list[Any]] = {key: [None] * len(days) for key, _, _ in METRICS}
    for row in fetch_daily_window(patient_id, start_day=start_day, end_day=end_day):
        # The query is bounded to the window, so every row has a slot. A
        # KeyError here means that invariant broke, which should be loud.
        slot = index[row.day]
        payload = row.payload if isinstance(row.payload, dict) else {}
        for key, family, field in METRICS:
            if family == row.family:
                series[key][slot] = payload.get(field)

    return {
        "connection_status": status,
        "last_error": last_error,
        "last_synced_at": _iso(state.last_synced_at if state else None),
        "link_sent_at": _iso(state.last_link_sent_at if state else None),
        "range": f"{range_days}d",
        "days": [day.isoformat() for day in days],
        "series": series,
        "latest": {key: _latest(values, days) for key, values in series.items()},
        "averages": {key: _average(values) for key, values in series.items()},
        "has_data": any(value is not None for values in series.values() for value in values),
        "disclaimer": DISCLAIMER,
    }


def _status(*, has_tokens: bool, last_error: str, link_pending: bool) -> str:
    if has_tokens and last_error in _RECONNECT_ERRORS:
        return STATUS_EXPIRED
    if has_tokens:
        return STATUS_CONNECTED
    if link_pending:
        return STATUS_LINK_PENDING
    return STATUS_DISCONNECTED


def _latest(values: list[Any], days: list[dt.date]) -> dict[str, Any] | None:
    """The most recent non-null value in the window, with its day."""
    for value, day in zip(reversed(values), reversed(days)):
        if value is not None:
            return {"value": value, "day": day.isoformat()}
    return None


def _average(values: list[Any]) -> float | None:
    numbers = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), 1)


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None
