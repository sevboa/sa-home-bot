from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.config import Settings
from sa_home_bot.llm.service import LlmService
from sa_home_bot.proto.emitter import DeferredEmitter
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_llm(settings: Settings) -> None:
    emitter = DeferredEmitter()
    service = LlmService(settings, emit=emitter)
    server = ProtoServer(settings.llm.socket, service, token=settings.swarm.token)
    emitter.bind(server)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    idle_task = asyncio.create_task(service.idle_loop(), name="llm-idle-loop")
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
        await service.notify_restart()
        await server.stop()
        log.info("Служба llm остановлена чисто")
