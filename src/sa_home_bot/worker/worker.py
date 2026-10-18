from __future__ import annotations

import logging

from sa_home_bot.jobs.base import JobContext
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.worker.queue import DedupQueue

log = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, queue: DedupQueue, ctx: JobContext) -> None:
        self._queue = queue
        self._ctx = ctx

    async def run(self) -> None:
        log.info("JobWorker запущен")
        while True:
            job = await self._queue.get()
            if job is None:
                self._queue.task_done()
                break
            await self._execute(job)
            self._queue.task_done()
        log.info("JobWorker остановлен")

    async def _execute(self, job) -> None:
        run_id = await self._ctx.store.start_job_run(job.job_type, utc_now())
        try:
            result = await job.run(self._ctx)
            await self._ctx.store.finish_job_run(
                run_id, "ok", utc_now(), metrics_json=result.to_json()
            )
            log.info("Job %s завершён: %s", job.job_type, result.to_json())
        except Exception as exc:  # noqa: BLE001
            log.exception("Job %s упал", job.job_type)
            await self._ctx.store.finish_job_run(run_id, "error", utc_now(), error=str(exc))
