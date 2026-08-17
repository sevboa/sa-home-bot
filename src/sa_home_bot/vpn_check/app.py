"""Сборка и жизненный цикл службы vpn_check (отдельный процесс).

Минимальная служба по образцу net/app.py: proto-сервер поверх
VpnCheckService, без БД и планировщика — реактивная. Плюс свой
ServiceLink-клиент к локальной ноде (по образцу tasks/app.py) для
исходящего пуша результата проверки в vpn/report_check на jeeves.
"""

from __future__ import annotations

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
    # Обработчики сигналов — до start(): он ждёт появления своего адреса
    # (см. proto/server.py), и всё это время остановка иначе не обрабатывалась бы.
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    await server.start()
    log.info("Служба vpn_check запущена: сокет %s", settings.vpn_check.socket)

    try:
        await lifespan.wait()
    finally:
        log.info("Останов службы vpn_check...")
        await server.stop()
        await node_link.stop()
        log.info("Служба vpn_check остановлена чисто")
