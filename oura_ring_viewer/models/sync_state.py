"""Per-patient sync watermark, error state, and pending-link tracking."""

# mypy: disable-error-code="var-annotated"

from django.db.models import (
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    UniqueConstraint,
)

from canvas_sdk.v1.data.base import CustomModel


class OuraSyncState(CustomModel):
    """One row per patient. Tracks sync watermark plus connect-link state."""

    patient_id = CharField(max_length=64)
    last_synced_at = DateTimeField(null=True, blank=True)
    last_day_synced = DateField(null=True, blank=True)
    last_error = CharField(max_length=512, blank=True, default="")
    last_error_at = DateTimeField(null=True, blank=True)
    last_link_sent_at = DateTimeField(null=True, blank=True)
    last_link_nonce = CharField(max_length=64, blank=True, default="")
    link_pending = BooleanField(default=False)

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["patient_id"],
                name="ourasyncstate_unique_patient",
            ),
        ]
