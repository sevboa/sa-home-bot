from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sa_home_bot.config import Settings
from sa_home_bot.jobs.housekeeping import HousekeepingJob
from sa_home_bot.jobs.scan import SensorScanJob
from sa_home_bot.jobs.smart import SmartScanJob
from sa_home_bot.worker.queue import DedupQueue

log = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="UTC")


def register_jobs(scheduler: AsyncIOScheduler, queue: DedupQueue, config: Settings) -> None:
    schedule = config.schedule
    jobs = (
        ("sensor_scan", schedule.scan_cron, SensorScanJob),
        ("smart_scan", schedule.smart_cron, SmartScanJob),
        ("housekeeping", schedule.housekeeping_cron, HousekeepingJob),
    )
    for job_id, cron, job_class in jobs:
        scheduler.add_job(
            _enqueuer(queue, job_class),
            CronTrigger.from_crontab(cron, timezone="UTC"),
            id=job_id,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
    log.info(
        "Запланировано: scan=%s, smart=%s, housekeeping=%s",
        schedule.scan_cron,
        schedule.smart_cron,
        schedule.housekeeping_cron,
    )


def _enqueuer(queue: DedupQueue, job_class: type):
    async def enqueue() -> None:
        await queue.put(job_class())

    return enqueue
