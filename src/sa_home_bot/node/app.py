"""Сборка и жизненный цикл сервиса ноды (супервизора).

Единственный systemd-юнит — у ноды: она поднимает назначенные службы
(monitor, telegram-bot) дочерними процессами, рестартит упавших и отдаёт
статус/управление по протоколу v0. События жизненного цикла служб уходят
broadcast'ом подключённым клиентам (nodectl events).
"""

from __future__ import annotations

import logging

from sa_home_bot.config import Settings
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)


async def run_node(settings: Settings, config_path: str | None = None) -> None:
    # Сервер создаётся до супервизора: emit замыкается на его broadcast.
    server: ProtoServer | None = None

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    supervisor = Supervisor(
        settings.node.assignments,
        config_path,
        emit=emit,
        restart_delay_s=settings.node.restart_delay_s,
        stop_timeout_s=settings.node.stop_timeout_s,
    )
    if not supervisor.services:
        log.warning("Нет ни одного валидного назначения — нода работает вхолостую")

    server = ProtoServer(settings.node.socket, NodeService(supervisor))
    await server.start()
    await supervisor.start_all()

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info(
        "Нода запущена: службы [%s], сокет %s",
        ", ".join(supervisor.services),
        settings.node.socket,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов ноды...")
        await supervisor.stop_all()
        await server.stop()
        log.info("Нода остановлена чисто")
