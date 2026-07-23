"""Сборка и жизненный цикл службы llm (отдельный процесс, деплоится на winpc).

Как apps/torrents — минимальный proto-сервер, плюс один фоновый таск
(идле-таймер, см. LlmService.idle_loop).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.config import Settings
from sa_home_bot.llm.service import LlmService
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_llm(settings: Settings) -> None:
    # server создаётся после service (см. node/app.py::run_node — тот же
    # приём) — emit замыкается на переменную server, реально дёргается уже
    # после server.start().
    server: ProtoServer | None = None

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    service = LlmService(settings, emit=emit)
    server = ProtoServer(settings.llm.socket, service, token=settings.swarm.token)
    await server.start()
    idle_task = asyncio.create_task(service.idle_loop(), name="llm-idle-loop")

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info(
        "Служба llm запущена: модель %s, сокет %s", settings.llm.model, settings.llm.socket
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы llm...")
        idle_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await idle_task
        await server.stop()
        log.info("Служба llm остановлена чисто")
