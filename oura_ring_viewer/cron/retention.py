"""Daily enforcement of the 90 day retention limit.

Sync also purges old rows, but only for the patient being synced. Without this
task, a patient whose care team stopped syncing (for example because the
patient revoked access in Oura) would keep their data indefinitely. Running
daily across all patients makes the retention limit unconditional.
"""

import datetime as dt

from canvas_sdk.effects import Effect
from canvas_sdk.handlers.cron_task import CronTask
from logger import log

from oura_ring_viewer.services.storage import purge_expired


class OuraRetentionTask(CronTask):
    """Deletes Oura summaries older than the retention horizon, daily."""

    # 03:17 UTC daily: off the hour, so it does not pile onto other plugins'
    # top-of-the-hour schedules.
    SCHEDULE = "17 3 * * *"

    def execute(self) -> list[Effect]:
        """Purge expired rows for every patient."""
        deleted = purge_expired(today=dt.datetime.now(dt.timezone.utc).date())
        if deleted:
            log.info(f"oura retention: deleted {deleted} expired daily summaries")
        return []
