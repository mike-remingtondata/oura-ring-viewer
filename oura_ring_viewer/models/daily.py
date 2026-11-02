"""Daily Oura summaries: one row per patient per day per metric family.

Deliberately coarse. Oura exposes intraday series (five-minute heart rate,
for example), but v1 stores daily granularity only. This keeps the table
bounded for the Canvas database-performance review: a 90 day retention
window over four metric families is at most 360 rows per patient.
"""

# mypy: disable-error-code="var-annotated"

from django.db.models import (
    CharField,
    DateField,
    DateTimeField,
    Index,
    JSONField,
    UniqueConstraint,
)

from canvas_sdk.v1.data.base import CustomModel


class OuraDaily(CustomModel):
    """One row per (patient, day, family).

    ``family`` is one of ``settings.FAMILIES``. ``payload`` holds the metrics
    for that family under plugin-defined keys produced by
    ``services/normalize.py``; raw Oura responses are never stored. Keeping
    the metrics in one JSON column means a new metric does not need a schema
    migration, and the unique constraint makes re-syncing an overlapping
    range idempotent.
    """

    patient_id = CharField(max_length=64)
    day = DateField()
    family = CharField(max_length=32)
    payload = JSONField(default=dict)
    fetched_at = DateTimeField()

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["patient_id", "day", "family"],
                name="ouradaily_unique_patient_day_family",
            ),
        ]
        indexes = [
            Index(fields=["patient_id", "day"], name="ouradaily_patient_day_idx"),
        ]
