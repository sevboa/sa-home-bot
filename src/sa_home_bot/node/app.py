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

from sa_home_bot.config import Settings, SwarmNodeConfig
from sa_home_bot.node import update as node_update
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.service import EVENT_NODE_JOINED, NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import MSG_EVENT, Envelope, ProtoError
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

# Локальные службы со своим proto-сервером, к которым нода умеет
# проксировать (telegram-bot — клиент, своего сервера у него нет).
def local_service_endpoints(settings: Settings) -> dict[str, str]:
    return {
        "monitor": settings.monitor.socket,
        "apps": settings.apps.socket,
        "torrents": settings.torrents.socket,
    }


_MAX_SEEN_EVENTS = 512  # с запасом — событий (join/update_finished) мало, часты не бывают


class SeenEvents:
    """Ограниченный набор недавно виденных id событий — защита от штормов
    ретрансляции.

    Живой инцидент 2026-07-17: в полносвязном рое из 3 нод (alfred,
    arch-t480, winpc — каждая напрямую связана с каждой) единственная
    проверка в `_relay_peer_event` («не эхо ли это моего же события»)
    ловит только прямой возврат к источнику, но НЕ повторные копии,
    приходящие от РАЗНЫХ соседей одним и тем же путём через треугольник —
    каждый узел ретранслирует КАЖДУЮ полученную копию по всем своим рёбрам
    заново, без счётчика «уже переслал». Событие размножается лавинообразно
    (без верхней границы, кроме размера графа) — Telegram забанил бота
    rate-limit'ом (429, retry-after 578с) примерно через 7 минут шторма.
    `env.id` — uuid4, генерируется один раз в `make_event()` и не меняется
    при ретрансляции (`broadcast_envelope` пересылает тот же `Envelope`) —
    надёжный ключ дедупа.
    """

    def __init__(self, maxsize: int = _MAX_SEEN_EVENTS) -> None:
        self._maxsize = maxsize
        self._ids: dict[str, None] = {}

    def seen(self, event_id: str) -> bool:
        """True, если уже видели этот id — иначе запоминает и возвращает False."""
        if event_id in self._ids:
            return True
        self._ids[event_id] = None
        if len(self._ids) > self._maxsize:
            self._ids.pop(next(iter(self._ids)))
        return False


def build_router(
    settings: Settings,
    node_id: str,
    assignments: list[str],
    extra_peers: list[SwarmNodeConfig],
    on_peer_event,
) -> NodeRouter:
    """Маршрутизатор: пиры из [[swarm.nodes]] ∪ персистентного состояния
    (join, этап 18) + локальные службы из назначений.

    ``assignments`` — эффективный набор (TOML ∪ персистентное состояние
    ноды), не только `settings.node.assignments` — см. `node/state.py`.
    """
    peer_configs: dict[str, SwarmNodeConfig] = {n.id: n for n in settings.swarm.nodes}
    for p in extra_peers:
        peer_configs.setdefault(p.id, p)
    peers = {
        pid: PeerLink(pid, cfg.endpoint, token=settings.swarm.token, on_event=on_peer_event)
        for pid, cfg in peer_configs.items()
        if pid != node_id  # свой id в списке — не пир
    }
    local = {
        name: PeerLink(name, endpoint, token=settings.swarm.token)
        for name, endpoint in local_service_endpoints(settings).items()
        if name in assignments
    }
    return NodeRouter(node_id, peers=peers, local_services=local)


def update_source_for_this_platform() -> str | None:
    """origin_repo_url() — умение (check_update/update) объявляется, если
    пакет поставлен из git, независимо от ОС.

    На win32 сама переустановка (`NodeService._update()`) не зовёт
    pipx_reinstall в процессе — `pipx install --force` там пытается
    перезаписать sa-home-node.exe и venv-DLL, которые держит открытыми ЭТОТ
    ЖЕ работающий процесс (WinError 5/32, живые находки 2026-07-17/
    2026-07-19). Вместо этого она дёргает Windows-задачу планировщика
    (deploy/win-auto-update.ps1, `node_update.trigger_scheduled_task()`) —
    та делает честный стоп→install→старт извне.
    """
    return node_update.origin_repo_url()


def _remember_peer(state: NodeState, node_id: str, endpoint: str) -> None:
    """Персистентный справочник пиров: только id+endpoint, не полный конфиг."""
    others = [p for p in state.peers if p.id != node_id]
    state.peers = [*others, SwarmNodeConfig(id=node_id, endpoint=endpoint)]


async def _relay_peer_event(
    env: Envelope,
    *,
    node_id: str,
    router: NodeRouter,
    state: NodeState,
    state_path: str,
    token: str,
    on_peer_event,
    server: ProtoServer | None,
    seen: SeenEvents,
) -> None:
    """Ретрансляция события пира своим клиентам + замыкание сетки (этап 18).

    Событие с собственным ``src.node`` — эхо от соседа, ему тут делать
    нечего. ``node_joined`` от уже связанного пира — авто-подключиться к
    новому узлу тем же путём, каким мы сами узнаём о событиях: так третий
    узел, не участвовавший в handshake напрямую, всё равно достраивает
    полную сетку за один хоп репликации события, без отдельного каталога.

    ``seen`` — дедуп по ``env.id``: в связном рое (≥3 узла, каждый с каждым)
    одно событие приходит несколькими путями, а без дедупа каждый узел
    ретранслирует КАЖДУЮ полученную копию заново — лавина (см. SeenEvents).
    """
    if env.src is not None and env.src.node == node_id:
        return
    if seen.seen(env.id):
        return
    if env.type == MSG_EVENT and env.payload.get("event") == EVENT_NODE_JOINED:
        data = env.payload.get("data", {})
        new_id, new_endpoint = data.get("node_id"), data.get("endpoint")
        if new_id and new_endpoint and new_id != node_id and new_id not in router.peers:
            link = PeerLink(new_id, new_endpoint, token=token, on_event=on_peer_event)
            await router.add_peer(link)
            _remember_peer(state, new_id, new_endpoint)
            state.save(state_path)
            log.info("Рой: авто-подключение к %s (%s) по node_joined", new_id, new_endpoint)
    if server is not None:
        await server.broadcast_envelope(env)


async def run_node(settings: Settings, config_path: str | None = None) -> bool:
    """Запустить ноду; вернуть True, если перед выходом запрошен само-рестарт
    (см. `restart_node` — cli.main() тогда делает os.execv на том же PID)."""
    # Сервер создаётся до супервизора: emit замыкается на его broadcast.
    server: ProtoServer | None = None
    node_id = settings.node.id or socket.gethostname()
    lifespan = Lifespan()
    restart_requested = False
    seen_events = SeenEvents()

    def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        log.warning("Нода %s: запрошен само-рестарт", node_id)
        lifespan.trigger()

    async def emit(event_type: str, data: dict) -> None:
        if server is not None:
            await server.broadcast_event(event_type, data)

    async def on_peer_event(env: Envelope) -> None:
        await _relay_peer_event(
            env,
            node_id=node_id,
            router=router,
            state=state,
            state_path=settings.node.state_path,
            token=settings.swarm.token,
            on_peer_event=on_peer_event,
            server=server,
            seen=seen_events,
        )

    # assignments в TOML — стартовый набор, не единственный источник:
    # состояние из assign/unassign в рантайме (nodectl/бот) переживает
    # рестарт через state_path (node/state.py), а не только через TOML.
    # То же для пиров: [[swarm.nodes]] — стартовый список, join (этап 18)
    # добавляет к нему динамически.
    state = NodeState.load(settings.node.state_path)
    effective_assignments = sorted(set(settings.node.assignments) | set(state.assignments))

    supervisor = Supervisor(
        effective_assignments,
        config_path,
        emit=emit,
        restart_delay_s=settings.node.restart_delay_s,
        stop_timeout_s=settings.node.stop_timeout_s,
    )
    if not supervisor.services:
        log.warning("Нет ни одного валидного назначения — нода работает вхолостую")

    router = build_router(settings, node_id, effective_assignments, state.peers, on_peer_event)
    # Нода слушает socket (локальные фронтенды) и, если задан, listen —
    # TCP для пиров роя.
    endpoints = [settings.node.socket]
    if settings.node.listen:
        endpoints.append(settings.node.listen)
    node_service = NodeService(
        supervisor,
        router,
        node_id=node_id,
        restart_node=request_restart,
        state=state,
        state_path=settings.node.state_path,
        local_service_endpoints=local_service_endpoints(settings),
        swarm_token=settings.swarm.token,
        own_endpoint=settings.node.listen,
        emit=emit,
        # Дешёвая файловая проверка (без сети) — editable/не-git установка
        # (dev-чекаут) или win32 дают None, и check_update/update не
        # объявляются (см. update_source_for_this_platform).
        update_source=update_source_for_this_platform(),
    )
    server = ProtoServer(endpoints, node_service, token=settings.swarm.token, router=router.route)
    await server.start()
    for link in (*router.peers.values(), *router.local_services.values()):
        await link.start()
    await supervisor.start_all()

    # Первый запуск с заданным swarm.join и ещё пустым списком пиров:
    # разовый bootstrap через тот же NodeService.join(), что и `nodectl join`
    # (принцип «сначала действие ноды») — дальше рой сам разъедется (join
    # соседа ретранслирует node_joined остальным — «замыкание сетки» выше).
    # Повторные рестарты join не повторяют — полагаемся на persisted-список.
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

    lifespan.install_signal_handlers()
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
        await supervisor.stop_all()
        for link in (*router.peers.values(), *router.local_services.values()):
            await link.stop()
        await server.stop()
        log.info("Нода остановлена чисто")
    return restart_requested
