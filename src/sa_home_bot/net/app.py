"""Сборка и жизненный цикл службы net (отдельный процесс).

Минимальная служба по образцу apps: proto-сервер поверх NetService, без БД и
планировщика. Состояние (живость SearXNG) читается на каждый запрос.
"""

from __future__ import annotations

import logging

from sa_home_bot.config import Settings
from sa_home_bot.net.service import NetService
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_net(settings: Settings) -> None:
    service = NetService(settings)
    server = ProtoServer(settings.net.socket, service, token=settings.swarm.token)
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info(
        "Служба net запущена: SearXNG %s, сокет %s",
        settings.net.searxng_url,
        settings.net.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы net...")
        await server.stop()
        log.info("Служба net остановлена чисто")
