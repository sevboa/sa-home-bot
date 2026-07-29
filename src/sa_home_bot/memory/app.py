"""Сборка и жизненный цикл службы memory (отдельный процесс).

По образцу tasks: proto-сервер поверх MemoryService плюс своя БД (общая
схема проекта, см. db/migrations.py — таблица facts объявлена там же).
"""

from __future__ import annotations

import logging

from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.memory.service import MemoryService
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_memory(settings: Settings) -> None:
    db = Database(settings.memory.db_path)
    await db.open()
    await apply_migrations(db)
    service = MemoryService(settings, db)
    server = ProtoServer(settings.memory.socket, service, token=settings.swarm.token)
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info(
        "Служба memory запущена: БД %s, сокет %s",
        settings.memory.db_path,
        settings.memory.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы memory...")
        await server.stop()
        await db.close()
        log.info("Служба memory остановлена чисто")
