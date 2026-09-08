"""Сборка и жизненный цикл службы ``reality`` (отдельный процесс, на ноде роя
с белым IP — сейчас wooster).

По образцу ``vpn/app.py``: proto-сервер поверх ``RealityService`` плюс своя
БД (общая схема проекта, отдельный файл). Фоновый цикл — сэмплер трафика
(``usage_loop``). При старте один раз сводим список юзеров xray с БД
(``reconcile``) — после рестарта xray inbound пуст, а БД помнит всех
активных; неудача (xray ещё не поднялся) не должна мешать службе стартовать.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.reality.service import RealityService
from sa_home_bot.reality.xray import RealXrayBackend
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_reality(settings: Settings) -> None:
    db = Database(settings.reality.db_path)
    await db.open()
    await apply_migrations(db)

    backend = RealXrayBackend(settings.reality.api_addr, settings.reality.inbound_tag)

    server: ProtoServer | None = None

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    service = RealityService(settings, db, backend, emit)
    await service.backfill_server()
    server = ProtoServer(settings.reality.socket, service, token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()

    with contextlib.suppress(Exception):
        await service.reconcile()

    usage_task = asyncio.create_task(service.usage_loop(), name="reality-usage-loop")
    log.info(
        "Служба reality запущена: xray api %s, сокет %s",
        settings.reality.api_addr,
        settings.reality.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы reality...")
        usage_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await usage_task
        await server.stop()
        await db.close()
        log.info("Служба reality остановлена чисто")
