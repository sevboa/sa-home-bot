"""Сборка и жизненный цикл службы graph_memory (отдельный процесс, mycraft).

По образцу memory/app.py (своя БД + ProtoServer) плюс фоновый цикл обработки
очереди эпизодов, как у tasks/app.py (prewake_task/fire_task) — здесь один
цикл, _ingest_loop (graph_memory/service.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.graph_memory.service import GraphMemoryService
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_graph_memory(settings: Settings) -> None:
    db = Database(settings.graph_memory.db_path)
    await db.open()
    await apply_migrations(db)
    service = GraphMemoryService(settings, db)
    server = ProtoServer(settings.graph_memory.socket, service, token=settings.swarm.token)
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()

    ingest_task = service.start_ingest_loop()
    log.info(
        "Служба graph_memory запущена: БД %s, сокет %s",
        settings.graph_memory.db_path,
        settings.graph_memory.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы graph_memory...")
        ingest_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ingest_task
        await server.stop()
        await service.aclose()
        await db.close()
        log.info("Служба graph_memory остановлена чисто")
