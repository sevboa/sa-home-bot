"""HousekeepingJob — ночная уборка: подрезать историю job_runs."""

from __future__ import annotations

from sa_home_bot.jobs.base import JobContext, JobResult

DEDUP_KEY = "housekeeping"
JOB_TYPE = "housekeeping"
KEEP_LAST_RUNS = 500
# Потолок истории показаний на компонент (с запасом над типичным baseline_window).
KEEP_READINGS_PER_COMPONENT = 5000
# Потолок журнала событий роя (db/schema.sql::swarm_events, bot/node_events.py).
KEEP_LAST_SWARM_EVENTS = 500


class HousekeepingJob:
    @property
    def dedup_key(self) -> str:
        return DEDUP_KEY

    @property
    def job_type(self) -> str:
        return JOB_TYPE

    async def run(self, ctx: JobContext) -> JobResult:
        pruned = await ctx.store.prune_job_runs(keep_last=KEEP_LAST_RUNS)
        pruned_readings = await ctx.store.prune_readings(KEEP_READINGS_PER_COMPONENT)
        pruned_events = await ctx.store.prune_swarm_events(KEEP_LAST_SWARM_EVENTS)
        return JobResult(
            extra={
                "pruned_job_runs": pruned,
                "pruned_readings": pruned_readings,
                "pruned_swarm_events": pruned_events,
            }
        )
