from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence

from sa_home_bot import __version__
from sa_home_bot.backup.identity import replicator_hooks as identity_replicator_hooks
from sa_home_bot.config import Settings, SwarmNodeConfig
from sa_home_bot.node import assignments as assignments_mod
from sa_home_bot.node import update as node_update
from sa_home_bot.node.discovery import SwarmDiscovery
from sa_home_bot.node.events import (
    EVENT_INSTANCE_CONFIG_CHANGED,
    EVENT_LLM_WENT_IDLE,
    EVENT_NODE_JOINED,
    EVENT_NODE_LEAVING,
    EVENT_RESTART_APPLIED,
)
from sa_home_bot.node.instances import InstanceStore, instances_dir
from sa_home_bot.node.lease import LeaseManager
from sa_home_bot.node.peers import LOCAL_HEARTBEAT_TIMEOUT_S, NodeRouter, PeerLink
from sa_home_bot.node.replication import ConfigReplicator
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.node.watch import PresenceWatcher
from sa_home_bot.proto.emitter import DeferredEmitter
from sa_home_bot.proto.endpoints import merge_endpoints
from sa_home_bot.proto.messages import MSG_EVENT, Envelope, ProtoError
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.services import registry
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.utils.shutdown import STEP_TIMEOUT_S, ShutdownBudget, arm_hard_exit

log = logging.getLogger(__name__)

SHUTDOWN_BUDGET_S = 20.0

HARD_EXIT_MARGIN_S = 15.0

LinkFactory = Callable[[str, "str | Sequence[str]"], PeerLink]

SEEN_EVENT_IDS_CAPACITY = 2048


MAX_RELAY_HOPS = 8


class SeenEvents:
    def __init__(self, maxsize: int = SEEN_EVENT_IDS_CAPACITY) -> None:
        self._maxsize = maxsize
        self._ids: dict[str, None] = {}

    def seen(self, event_id: str) -> bool:
        if event_id in self._ids:
            return True
        self._ids[event_id] = None
        if len(self._ids) > self._maxsize:
            self._ids.pop(next(iter(self._ids)))
        return False


def make_link_factories(
    settings: Settings, node_id: str, on_peer_event, on_local_event, on_endpoints=None
) -> tuple[LinkFactory, LinkFactory]:

    def make_peer_link(peer_id: str, endpoint: str | Sequence[str]) -> PeerLink:
        return PeerLink(
            peer_id,
            endpoint,
            token=settings.swarm.token,
            on_event=on_peer_event,
            on_endpoints=on_endpoints,
            self_node=node_id,
        )

    def make_local_link(service: str, endpoint: str | Sequence[str]) -> PeerLink:
        return PeerLink(
            service,
            endpoint,
            token=settings.swarm.token,
            on_event=on_local_event,
            heartbeat_timeout=LOCAL_HEARTBEAT_TIMEOUT_S,
        )

    return make_peer_link, make_local_link


def peer_candidates(
    nodes: Sequence[SwarmNodeConfig], extra: Sequence[SwarmNodeConfig]
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for cfg in (*extra, *nodes):
        known = out.setdefault(cfg.id, [])
        for value in (cfg.endpoint, *cfg.endpoints):
            if value and value not in known:
                known.append(value)
    return out


def peer_kinds(
    nodes: Sequence[SwarmNodeConfig], extra: Sequence[SwarmNodeConfig]
) -> dict[str, str]:
    out: dict[str, str] = {}
    for cfg in (*extra, *nodes):
        if cfg.kind and cfg.id not in out:
            out[cfg.id] = cfg.kind
    return out


def build_router(
    settings: Settings,
    node_id: str,
    assignments: list[str],
    extra_peers: list[SwarmNodeConfig],
    make_peer_link: LinkFactory,
    make_local_link: LinkFactory,
) -> NodeRouter:
    peers = {
        pid: make_peer_link(pid, candidates)
        for pid, candidates in peer_candidates(settings.swarm.nodes, extra_peers).items()
        if pid != node_id
    }
    for pid, node_kind in peer_kinds(settings.swarm.nodes, extra_peers).items():
        link = peers.get(pid)
        if link is not None and not link.node_kind:
            link.node_kind = node_kind
    assigned_services = {a.service for a in assignments_mod.parse_valid(assignments)}
    local = {
        name: make_local_link(name, endpoint)
        for name, endpoint in registry.service_endpoints(settings).items()
        if name in assigned_services
    }
    return NodeRouter(node_id, peers=peers, local_services=local)


ANNOUNCE_VERSION_RETRY_S = 5.0
ANNOUNCE_VERSION_WINDOW_S = 120.0


async def _announce_version_if_changed(
    state: NodeState,
    state_path: str,
    broadcast: Callable[[str, dict], Awaitable[int]],
    *,
    retry_s: float = ANNOUNCE_VERSION_RETRY_S,
    window_s: float = ANNOUNCE_VERSION_WINDOW_S,
) -> None:
    if state.last_known_version is None:
        state.last_known_version = __version__
        state.save(state_path)
        return
    if state.last_known_version == __version__:
        return
    data = {"from": state.last_known_version, "to": __version__}
    state.last_known_version = __version__
    state.save(state_path)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + window_s
    while True:
        if await broadcast(EVENT_RESTART_APPLIED, data) > 0:
            return
        if loop.time() >= deadline:
            log.warning(
                "restart_applied (%s -> %s): не удалось разослать за %.0fс — "
                "нет ни одного подключённого получателя",
                data["from"],
                data["to"],
                window_s,
            )
            return
        await asyncio.sleep(retry_s)


async def _relay_peer_event(
    env: Envelope,
    *,
    node_id: str,
    router: NodeRouter,
    state: NodeState,
    state_path: str,
    make_peer_link: LinkFactory,
    server: ProtoServer | None,
    seen: SeenEvents,
    is_local: bool = False,
    replicator: ConfigReplicator | None = None,
) -> None:
    if not is_local and env.src is not None and env.src.node == node_id:
        return
    if seen.seen(env.id):
        return
    if env.hops >= MAX_RELAY_HOPS:
        log.warning(
            "Рой: событие %s (%s) пережило %d ретрансляций — дропаю (циркуляция?)",
            env.payload.get("event"),
            env.id,
            env.hops,
        )
        return
    if env.type == MSG_EVENT and env.payload.get("event") == EVENT_NODE_JOINED:
        data = env.payload.get("data", {})
        new_id, new_endpoint = data.get("node_id"), data.get("endpoint")
        if new_id and new_endpoint and new_id != node_id and new_id not in router.peers:
            candidates = merge_endpoints(new_endpoint, data.get("endpoints"))
            link = make_peer_link(new_id, candidates)
            await router.add_peer(link)
            state.remember_peer(new_id, new_endpoint, candidates)
            state.save(state_path)
            log.info("Рой: авто-подключение к %s (%s) по node_joined", new_id, candidates)
    if (
        replicator is not None
        and env.type == MSG_EVENT
        and env.payload.get("event") == EVENT_INSTANCE_CONFIG_CHANGED
    ):
        await replicator.on_config_changed(env)
    if server is not None:
        await server.broadcast_envelope(dataclasses.replace(env, hops=env.hops + 1))
    if env.type == MSG_EVENT and env.payload.get("event") == EVENT_NODE_LEAVING:
        leaving = env.payload.get("data", {}).get("node") or (env.src.node if env.src else None)
        left_link = router.peers.get(leaving) if leaving else None
        if left_link is not None:
            left_link.note_left()


async def run_node(settings: Settings, config_path: str | None = None) -> bool:
    emit = DeferredEmitter()
    server: ProtoServer | None = None
    replicator: ConfigReplicator | None = None
    lease: LeaseManager | None = None
    node_service: NodeService | None = None
    node_id = settings.node.id or socket.gethostname()
    lifespan = Lifespan()
    restart_requested = False
    seen_events = SeenEvents()

    def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        log.warning("Нода %s: запрошен само-рестарт", node_id)
        lifespan.trigger()

    async def relay(env: Envelope, *, is_local: bool) -> None:
        await _relay_peer_event(
            env,
            node_id=node_id,
            router=router,
            state=state,
            state_path=settings.node.state_path,
            make_peer_link=make_peer_link,
            server=server,
            seen=seen_events,
            is_local=is_local,
            replicator=None if is_local else replicator,
        )

    async def on_peer_event(env: Envelope) -> None:
        await relay(env, is_local=False)

    async def on_local_event(env: Envelope) -> None:
        await relay(env, is_local=True)
        if env.payload.get("event") == EVENT_LLM_WENT_IDLE and node_service is not None:
            asyncio.create_task(node_service.maybe_auto_poweroff_idle(), name="idle-poweroff-check")

    def on_peer_endpoints(peer_id: str, endpoints: list[str]) -> None:
        if not endpoints:
            return
        state.remember_peer(peer_id, endpoints[0], endpoints)
        state.save(settings.node.state_path)

    make_peer_link, make_local_link = make_link_factories(
        settings, node_id, on_peer_event, on_local_event, on_peer_endpoints
    )

    state = NodeState.load(settings.node.state_path)
    effective_assignments = sorted(set(settings.node.assignments) | set(state.assignments))

    def on_fenced(slot_name: str) -> None:
        if lease is not None:
            lease.note_fenced(slot_name)

    def on_spawned(slot_name: str) -> None:
        if lease is not None:
            lease.set_ready(slot_name, False)

    supervisor = Supervisor(
        effective_assignments,
        config_path,
        emit=emit,
        restart_delay_s=settings.node.restart_delay_s,
        stop_timeout_s=settings.node.stop_timeout_s,
        on_fenced=on_fenced,
        on_spawned=on_spawned,
    )
    if not supervisor.services:
        log.warning("Нет ни одного валидного назначения — нода работает вхолостую")

    router = build_router(
        settings, node_id, effective_assignments, state.peers, make_peer_link, make_local_link
    )
    packages_dir = instances_dir(config_path)
    replicator = None
    if packages_dir is not None:
        package_store = InstanceStore(packages_dir, node_id)
        replicator = ConfigReplicator(
            node_id,
            package_store,
            supervisor=supervisor,
            router=router,
            emit=emit,
            **identity_replicator_hooks(
                settings,
                package_store,
                node_id,
                vpn_assigned=assignments_mod.has_service(effective_assignments, "vpn"),
            ),
        )
    lease = LeaseManager(
        node_id,
        settings.node.kind,
        supervisor,
        router=router,
        emit=emit,
        grace_s=settings.swarm.failover_grace_s,
        ready_timeout_s=settings.swarm.superior_ready_timeout_s,
    )
    endpoints = [settings.node.socket, *settings.node.listen]
    node_service = NodeService(
        supervisor,
        router,
        node_id=node_id,
        restart_node=request_restart,
        state=state,
        state_path=settings.node.state_path,
        local_service_endpoints=registry.service_endpoints(settings),
        swarm_token=settings.swarm.token,
        own_endpoint=settings.node.listen[0] if settings.node.listen else "",
        advertise=lambda: server.advertised_endpoints() if server is not None else [],
        emit=emit,
        update_source=node_update.origin_repo_url(),
        node_kind=settings.node.kind,
        power_controllable=settings.node.power_controllable,
        idle_poweroff=settings.node.idle_poweroff,
        replicator=replicator,
        lease=lease,
        make_peer_link=make_peer_link,
        make_local_link=make_local_link,
    )

    def on_peer_connect(peer_node: str) -> None:
        link = router.peers.get(peer_node)
        if link is not None:
            link.reconnect_now("сосед подключился заново")

    lifespan.install_signal_handlers()

    server = ProtoServer(
        endpoints,
        node_service,
        token=settings.swarm.token,
        router=router.route,
        on_peer_connect=on_peer_connect,
    )
    emit.bind(server)
    await server.start()
    asyncio.create_task(
        _announce_version_if_changed(state, settings.node.state_path, server.broadcast_event),
        name="announce-version",
    )
    for link in (*router.peers.values(), *router.local_services.values()):
        await link.start()
    if replicator is not None:
        await replicator.start()
    await supervisor.start_all()
    await lease.start()

    watcher = PresenceWatcher(
        node_id,
        router,
        emit=emit,
        state=state,
        state_path=settings.node.state_path,
        down_after_s=settings.swarm.node_down_alert_after_s,
    )
    await watcher.start()

    discovery = SwarmDiscovery(
        node_id,
        router,
        token=settings.swarm.token,
        join=node_service.join,
        advertise=server.advertised_endpoints,
        cfg=settings.swarm.discovery,
        node_kind=settings.node.kind,
    )
    node_service.attach_discovery(discovery.state)
    await discovery.start()

    if settings.swarm.join and not state.peers:
        try:
            result = await node_service.join(settings.swarm.join)
            log.info(
                "swarm.join: присоединились через %s, новых пиров: %s",
                settings.swarm.join,
                result["peers_added"],
            )
        except ProtoError as exc:
            log.warning(
                "swarm.join: сосед %s недоступен (%s) — присоединюсь позже",
                settings.swarm.join,
                exc,
            )

    log.info(
        "Нода %s запущена: службы [%s], пиры [%s], endpoints [%s]",
        node_id,
        ", ".join(supervisor.services),
        ", ".join(router.peers) or "—",
        ", ".join(str(ep) for ep in server.endpoints),
    )

    try:
        await lifespan.wait()
    finally:
        log.info("Останов ноды...")
        budget = ShutdownBudget(total_s=settings.node.stop_timeout_s + SHUTDOWN_BUDGET_S)
        arm_hard_exit(budget.total_s + HARD_EXIT_MARGIN_S)
        farewell = emit(EVENT_NODE_LEAVING, {"node": node_id, "kind": settings.node.kind})
        await budget.step("прощание с роем", farewell)
        await budget.step("присутствие соседей", watcher.stop())
        await budget.step("маячок discovery", discovery.stop())
        await budget.step("аренда лидерства", lease.stop())
        if replicator is not None:
            await budget.step("репликация настроек", replicator.stop())
        await budget.step(
            "останов служб",
            supervisor.stop_all(),
            timeout=settings.node.stop_timeout_s + STEP_TIMEOUT_S,
        )
        for link in (*router.peers.values(), *router.local_services.values()):
            await budget.step(f"линк {link.name}", link.stop())
        await budget.step("proto-сервер", server.stop())
        if budget.overdue:
            log.error("Нода остановлена с оговорками: не уложились [%s]", ", ".join(budget.overdue))
        else:
            log.info("Нода остановлена чисто")
    return restart_requested
