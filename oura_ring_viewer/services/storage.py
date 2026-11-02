"""Persistence helpers around the three custom data models.

Centralizes upsert and purge logic so handlers never reach into the ORM
directly. Datetimes are UTC; ``day`` values are the ring owner's local date
exactly as Oura reports them.

The plugin sandbox does not allow ``django.db.transaction``, so multi-step
operations are ordered to fail safe rather than wrapped atomically.
"""

import datetime as dt
from typing import Any, Iterable

from oura_ring_viewer.models import OuraDaily, OuraOAuthToken, OuraSyncState
from oura_ring_viewer.services.normalize import NormalizedDay
from oura_ring_viewer.services.settings import DAILY_RETENTION_DAYS


def upsert_tokens(
    patient_id: str,
    *,
    access_token_ciphertext: str,
    refresh_token_ciphertext: str,
    expires_at: dt.datetime,
    scopes: str,
    now: dt.datetime,
    is_initial_connection: bool,
) -> None:
    """Insert or update the token row for the patient."""
    defaults: dict[str, Any] = {
        "access_token": access_token_ciphertext,
        "refresh_token": refresh_token_ciphertext,
        "expires_at": expires_at,
        "last_refresh_at": now,
    }
    if scopes:
        defaults["scopes"] = scopes
    if is_initial_connection:
        defaults["connected_at"] = now
    OuraOAuthToken.objects.update_or_create(patient_id=patient_id, defaults=defaults)


def get_tokens(patient_id: str) -> OuraOAuthToken | None:
    """Return the raw stored token row for the patient, or None."""
    return OuraOAuthToken.objects.filter(patient_id=patient_id).first()


def delete_all_for_patient(patient_id: str) -> None:
    """Disconnect: remove every plugin row for the patient.

    Tokens are deleted first, so a failure partway through still leaves the
    patient effectively disconnected: with no tokens, no further sync can run.
    """
    OuraOAuthToken.objects.filter(patient_id=patient_id).delete()
    OuraSyncState.objects.filter(patient_id=patient_id).delete()
    OuraDaily.objects.filter(patient_id=patient_id).delete()


def upsert_sync_state(patient_id: str, **fields: Any) -> OuraSyncState:
    """Update or create the sync-state row, writing only the supplied fields."""
    state, _ = OuraSyncState.objects.update_or_create(patient_id=patient_id, defaults=fields)
    return state


def get_sync_state(patient_id: str) -> OuraSyncState | None:
    """Return the sync-state row, or None if the patient never connected."""
    return OuraSyncState.objects.filter(patient_id=patient_id).first()


def store_daily(patient_id: str, rows: Iterable[NormalizedDay], *, now: dt.datetime) -> int:
    """Upsert normalized daily rows for the patient. Returns rows written.

    De-duplicates on ``(day, family)`` first, last one winning: a Postgres
    ``ON CONFLICT`` upsert cannot touch the same row twice in one statement.
    A 90 day sync across four families is at most 360 rows, comfortably one
    batch, so no chunking is needed.
    """
    by_key: dict[tuple[dt.date, str], OuraDaily] = {}
    for row in rows:
        by_key[(row.day, row.family)] = OuraDaily(
            patient_id=patient_id,
            day=row.day,
            family=row.family,
            payload=row.payload,
            fetched_at=now,
        )
    objects = list(by_key.values())
    if objects:
        OuraDaily.objects.bulk_create(
            objects,
            update_conflicts=True,
            unique_fields=["patient_id", "day", "family"],
            update_fields=["payload", "fetched_at"],
        )
    return len(objects)


def purge_old_daily(patient_id: str, *, today: dt.date) -> int:
    """Delete rows older than the retention horizon. Returns rows deleted."""
    cutoff = today - dt.timedelta(days=DAILY_RETENTION_DAYS)
    deleted, _ = OuraDaily.objects.filter(patient_id=patient_id, day__lt=cutoff).delete()
    return int(deleted)


def fetch_daily_window(
    patient_id: str, *, start_day: dt.date, end_day: dt.date
) -> list[OuraDaily]:
    """Rows with ``start_day <= day <= end_day``, ordered by day then family.

    Served from the local table, never from Oura, so opening the chart does not
    cost an API call. Hits the ``(patient_id, day)`` index.
    """
    return list(
        OuraDaily.objects.filter(
            patient_id=patient_id, day__gte=start_day, day__lte=end_day
        ).order_by("day", "family")
    )
