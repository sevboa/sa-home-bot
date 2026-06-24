"""Планировщик: cron-триггеры кладут job'ы в очередь.

В callback'е планировщика — только put в DedupQueue (ARCHITECTURE §4). Все
cron-job'ы: coalesce=True, max_instances=1 (инвариант §9.4).
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sa_home_bot.config import Settings
from sa_home_bot.jobs.housekeeping import HousekeepingJob
from sa_home_bot.jobs.scan import SensorScanJob
from sa_home_bot.worker.queue import DedupQueue

log = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="UTC")


def register_jobs(scheduler: AsyncIOScheduler, queue: DedupQueue, config: Settings) -> None:
    async def enqueue_scan() -> None:
        await queue.put(SensorScanJob())

    async def enqueue_housekeeping() -> None:
        await queue.put(HousekeepingJob())

    scheduler.add_job(
        enqueue_scan,
        CronTrigger.from_crontab(config.schedule.scan_cron, timezone="UTC"),
        id="sensor_scan",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    scheduler.add_job(
        enqueue_housekeeping,
        CronTrigger.from_crontab(config.schedule.housekeeping_cron, timezone="UTC"),
        id="housekeeping",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    log.info(
        "Запланировано: scan=%s, housekeeping=%s",
        config.schedule.scan_cron,
        config.schedule.housekeeping_cron,
    )
