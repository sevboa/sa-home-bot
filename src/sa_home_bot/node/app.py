"""Сборка и жизненный цикл сервиса ноды (супервизора).

Единственный systemd-юнит — у ноды: она поднимает назначенные службы
(monitor, telegram-bot) дочерними процессами, рестартит упавших и отдаёт
статус/управление по протоколу v0. События жизненного цикла служб уходят
broadcast'ом подключённым клиентам (nodectl events).

Нода же — точка входа роя: запросы с чужим ``dst`` пересылаются пирам из
``[[swarm.nodes]]`` или локальным службам (см. node/peers.py), события
пиров ретранслируются своим клиентам с сохранением src.
"""

from __future__ import annotations

import logging
import socket

from sa_home_bot.config import Settings
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import Envelope
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

def build_router(settings: Settings, node_id: str, on_peer_event) -> NodeRouter:
    """Маршрутизатор: пиры из [[swarm.nodes]] + локальные службы из назначений."""
    peers = {
        n.id: PeerLink(n.id, n.endpoint, token=settings.swarm.token, on_event=on_peer_event)
        for n in settings.swarm.nodes
        if n.id != node_id  # свой id в списке — не пир (общий конфиг роя)
    }
    # Локальные службы с proto-сервером, к которым нода умеет проксировать
    # (telegram-bot — клиент, своего сервера у него нет).
    proxied = {"monitor": settings.monitor.socket, "apps": settings.apps.socket}
    local = {
        name: PeerLink(name, endpoint, token=settings.swarm.token)
        for name, endpoint in proxied.items()
        if name in settings.node.assignments
    }
    return NodeRouter(node_id, peers=peers, local_services=local)


async def run_node(settings: Settings, config_path: str | None = None) -> None:
    # Сервер создаётся до супервизора: emit замыкается на его broadcast.
    server: ProtoServer | None = None
    node_id = socket.gethostname()

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    async def on_peer_event(env: Envelope) -> None:
        # Ретрансляция события пира своим клиентам (бот, nodectl). Событие
        # с собственным src.node — эхо от соседа, ему тут делать нечего.
        if env.src is not None and env.src.node == node_id:
            return
        if server is not None:
            await server.broadcast_envelope(env)

    supervisor = Supervisor(
        settings.node.assignments,
        config_path,
        emit=emit,
        restart_delay_s=settings.node.restart_delay_s,
        stop_timeout_s=settings.node.stop_timeout_s,
    )
    if not supervisor.services:
        log.warning("Нет ни одного валидного назначения — нода работает вхолостую")

    router = build_router(settings, node_id, on_peer_event)
    server = ProtoServer(
        settings.node.socket,
        NodeService(supervisor, router),
        token=settings.swarm.token,
        router=router.route,
    )
    await server.start()
    for link in (*router.peers.values(), *router.local_services.values()):
        await link.start()
    await supervisor.start_all()

    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info(
        "Нода %s запущена: службы [%s], пиры [%s], endpoint %s",
        node_id,
        ", ".join(supervisor.services),
        ", ".join(router.peers) or "—",
        server.endpoint,
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов ноды...")
        await supervisor.stop_all()
        for link in (*router.peers.values(), *router.local_services.values()):
            await link.stop()
        await server.stop()
        log.info("Нода остановлена чисто")
