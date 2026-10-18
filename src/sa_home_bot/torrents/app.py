from __future__ import annotations

import logging

from sa_home_bot.config import Settings
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.torrents.service import TorrentsService
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_torrents(settings: Settings) -> None:
    service = TorrentsService(settings)
    server = ProtoServer(settings.torrents.socket, service, token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info(
        "Служба torrents запущена: %d директорий, сокет %s",
        len(settings.torrents.save_dirs),
        settings.torrents.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы torrents...")
        await server.stop()
        log.info("Служба torrents остановлена чисто")
