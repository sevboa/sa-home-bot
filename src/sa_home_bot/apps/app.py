"""Сборка и жизненный цикл службы apps (отдельный процесс).

Минимальная служба: proto-сервер поверх AppsService, без БД и планировщика.
Состояние юнитов читается на каждый запрос — кэшировать нечего.
"""

from __future__ import annotations

import logging

from sa_home_bot.apps.service import AppsService
from sa_home_bot.config import Settings
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_apps(settings: Settings) -> None:
    service = AppsService(settings)
    server = ProtoServer(settings.apps.socket, service, token=settings.swarm.token)
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info(
        "Служба apps запущена: %d приложений, сокет %s",
        len(settings.apps.items),
        settings.apps.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы apps...")
        await server.stop()
        log.info("Служба apps остановлена чисто")
