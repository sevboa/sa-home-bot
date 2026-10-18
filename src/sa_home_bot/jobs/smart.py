from __future__ import annotations

import logging

from sa_home_bot.domain.smart import diff_smart
from sa_home_bot.jobs.base import JobContext, JobResult

log = logging.getLogger(__name__)

DEDUP_KEY = "smart-scan"
JOB_TYPE = "smart_scan"


class SmartScanJob:
    @property
    def dedup_key(self) -> str:
        return DEDUP_KEY

    @property
    def job_type(self) -> str:
        return JOB_TYPE

    async def run(self, ctx: JobContext) -> JobResult:
        snapshots = await ctx.sensors.read_smart_snapshots()
        sent = 0
        changes = 0
        for curr in snapshots:
            prev = await ctx.store.get_smart_snapshot(curr.component_id)
            change = diff_smart(prev, curr)
            if change is None:
                await ctx.store.save_smart_snapshot(curr)
                continue
            changes += 1
            result = await ctx.dispatcher.dispatch_smart(change)
            if not result.handled:
                log.warning(
                    "SMART-алерт по %s не доставлен — повтор на следующем прогоне",
                    change.component_id,
                )
                continue
            await ctx.store.save_smart_snapshot(curr)
            if result.delivered:
                sent += 1
        return JobResult(
            components_scanned=len(snapshots),
            transitions=changes,
            alerts_sent=sent,
        )
