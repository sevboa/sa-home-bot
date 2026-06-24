"""HousekeepingJob — ночная уборка: подрезать историю job_runs."""

from __future__ import annotations

from sa_home_bot.jobs.base import JobContext, JobResult

DEDUP_KEY = "housekeeping"
JOB_TYPE = "housekeeping"
KEEP_LAST_RUNS = 500


class HousekeepingJob:
    @property
    def dedup_key(self) -> str:
        return DEDUP_KEY

    @property
    def job_type(self) -> str:
        return JOB_TYPE

    async def run(self, ctx: JobContext) -> JobResult:
        pruned = await ctx.store.prune_job_runs(keep_last=KEEP_LAST_RUNS)
        return JobResult(extra={"pruned_job_runs": pruned})
