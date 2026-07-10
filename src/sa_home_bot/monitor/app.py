"""Сборка и жизненный цикл службы monitor (отдельный процесс).

Монитор владеет датчиками, порогами, планировщиком и собственной БД; наружу —
только протокол v0 (unix-сокет): get_state, command scan_now, поток событий.
Telegram он не знает; доставка людям — забота бота-клиента.
"""

from __future__ import annotations

import asyncio
import logging

from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.jobs.base import JobContext
from sa_home_bot.jobs.smart import SmartScanJob
from sa_home_bot.monitor.dispatch import ProtoEventDispatcher
from sa_home_bot.monitor.service import MonitorService
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.scheduler.setup import build_scheduler, register_jobs
from sa_home_bot.sensors.source import SensorSource
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.worker.queue import DedupQueue
from sa_home_bot.worker.worker import JobWorker

log = logging.getLogger(__name__)


async def run_monitor(settings: Settings) -> None:
    # 1. Своя БД монитора (не БД бота) + миграции.
    db = Database(settings.monitor.db_path)
    await db.open()
    await apply_migrations(db)
    store = Store(db)

    # 2. Датчики, очередь, proto-сервер.
    sensors = SensorSource(settings.sensors)
    queue = DedupQueue()
    service = MonitorService(settings, store, queue)
    server = ProtoServer(settings.monitor.socket, service, token=settings.swarm.token)
    await server.start()

    # 3. Worker: события уходят broadcast'ом по протоколу.
    ctx = JobContext(
        store=store,
        sensors=sensors,
        dispatcher=ProtoEventDispatcher(server),
        config=settings,
    )
    worker = JobWorker(queue, ctx)
    worker_task = asyncio.create_task(worker.run(), name="job-worker")

    # 4. Scheduler + разовый SMART-снимок на старте (baseline сразу).
    scheduler = build_scheduler()
    register_jobs(scheduler, queue, settings)
    scheduler.start()
    await queue.put(SmartScanJob())

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info("Монитор запущен, сокет %s", settings.monitor.socket)

    try:
        await lifespan.wait()
    finally:
        log.info("Останов монитора...")
        scheduler.shutdown(wait=False)
        await queue.stop()
        await worker_task
        await server.stop()
        await db.close()
        log.info("Монитор остановлен чисто")
