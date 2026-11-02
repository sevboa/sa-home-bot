from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.vpn_check.service import VpnCheckService

log = logging.getLogger(__name__)


async def run_vpn_check(settings: Settings) -> None:
    node_link = ServiceLink(
        settings.node.socket, token=settings.swarm.token, display_name="нода (vpn_check)"
    )
    await node_link.start()

    service = VpnCheckService(settings, node_link)
    server = ProtoServer(settings.vpn_check.socket, service, token=settings.swarm.token)
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info("Служба vpn_check запущена: сокет %s", settings.vpn_check.socket)

    series_task: asyncio.Task | None = None
    if settings.vpn_series.enabled:
        series_task = asyncio.create_task(service.series_loop(), name="vpn-series-loop")

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы vpn_check...")
        if series_task is not None:
            series_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await series_task
        await server.stop()
        await node_link.stop()
        log.info("Служба vpn_check остановлена чисто")
