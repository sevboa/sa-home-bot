"""Сборка и жизненный цикл службы vpn (отдельный процесс, обычно на jeeves).

По образцу tasks/app.py: proto-сервер поверх VpnService плюс своя БД (общая
схема проекта). Фоновый цикл — сэмплер трафика (usage_loop). При старте
пробуем один раз свести интерфейс с БД (reconcile) — после рестарта jeeves
awg0 пуст, а БД помнит всех активных пиров; неудача (sudoers ещё не
поставлен через ``nodectl fix``) не должна мешать службе подняться —
`get_state`/`describe` и хотя бы `issue` дальше сработают попыткой, а
диагноз ``ERR_NEEDS_PRIVILEGE`` объяснит, что делать.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.config import Settings
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.vpn.awg import RealAwgBackend
from sa_home_bot.vpn.service import VpnService

log = logging.getLogger(__name__)


async def run_vpn(settings: Settings) -> None:
    db = Database(settings.vpn.db_path)
    await db.open()
    await apply_migrations(db)

    backend = RealAwgBackend(settings.vpn.interface)

    server: ProtoServer | None = None

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    service = VpnService(settings, db, backend, emit)
    server = ProtoServer(settings.vpn.socket, service, token=settings.swarm.token)
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()

    with contextlib.suppress(Exception):
        await service.reconcile()

    usage_task = asyncio.create_task(service.usage_loop(), name="vpn-usage-loop")
    log.info(
        "Служба vpn запущена: интерфейс %s, сокет %s", settings.vpn.interface, settings.vpn.socket
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы vpn...")
        usage_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await usage_task
        await server.stop()
        await db.close()
        log.info("Служба vpn остановлена чисто")
