"""Tests for the daily retention task."""

import datetime as dt
from unittest.mock import MagicMock, patch

from cron_converter import Cron

from oura_ring_viewer.cron.retention import OuraRetentionTask
from oura_ring_viewer.models import OuraDaily
from oura_ring_viewer.services.settings import DAILY_RETENTION_DAYS

TODAY = dt.date(2026, 11, 2)
NOW = dt.datetime(2026, 11, 2, 3, 17, tzinfo=dt.timezone.utc)


def _row(patient_id: str, days_ago: int) -> None:
    OuraDaily.objects.create(
        patient_id=patient_id, day=TODAY - dt.timedelta(days=days_ago), family="sleep",
        payload={"score": 80}, fetched_at=NOW,
    )


def test_purges_expired_rows_for_every_patient_and_keeps_the_rest() -> None:
    for patient in ("p1", "p2"):
        _row(patient, DAILY_RETENTION_DAYS + 1)
        _row(patient, DAILY_RETENTION_DAYS)
        _row(patient, 0)

    with patch("oura_ring_viewer.cron.retention.dt") as fake_dt:
        fake_dt.datetime.now.return_value = NOW
        fake_dt.timezone = dt.timezone
        assert OuraRetentionTask(event=MagicMock()).execute() == []

    for patient in ("p1", "p2"):
        kept = sorted(OuraDaily.objects.filter(patient_id=patient).values_list("day", flat=True))
        assert kept == [TODAY - dt.timedelta(days=DAILY_RETENTION_DAYS), TODAY]


def test_runs_once_a_day() -> None:
    cron = Cron(OuraRetentionTask.SCHEDULE)
    schedule = cron.schedule(dt.datetime(2026, 11, 2, 0, 0, tzinfo=dt.timezone.utc))
    first, second = schedule.next(), schedule.next()
    assert (first.hour, first.minute) == (3, 17)
    assert second - first == dt.timedelta(days=1)
