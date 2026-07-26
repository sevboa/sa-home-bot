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
from sa_home_bot.node import assignments as assignments_mod
from sa_home_bot.node import update as node_update
from sa_home_bot.node.instances import InstanceStore, instances_dir
from sa_home_bot.node.lease import LeaseManager
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.replication import (
    EVENT_INSTANCE_CONFIG_CHANGED,
    ConfigReplicator,
)
from sa_home_bot.node.service import EVENT_NODE_JOINED, NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.node.watch import PresenceWatcher
from sa_home_bot.proto.messages import MSG_EVENT, Envelope, ProtoError
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.services import registry
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

# Локальные службы со своим proto-сервером, к которым нода умеет
# проксировать (telegram-bot — клиент, своего сервера у него нет).
# Состав — из реестра служб (services/registry.py), а не из списка здесь.
def local_service_endpoints(settings: Settings) -> dict[str, str]:
    return registry.service_endpoints(settings)


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


def _safe_parse_all(items: list[str]) -> list[assignments_mod.Assignment]:
    """Разобрать назначения, пропустив битые: об их непонятности уже сказал
    супервизор — второй раз ругаться на то же самое незачем."""
    parsed = []
    for item in items:
        try:
            parsed.append(assignments_mod.parse(item))
        except assignments_mod.AssignmentError:
            continue
    return parsed


def build_router(
    settings: Settings,
    node_id: str,
    assignments: list[str],
    extra_peers: list[SwarmNodeConfig],
    on_peer_event,
    on_local_event,
) -> NodeRouter:
    """Маршрутизатор: пиры из [[swarm.nodes]] ∪ персистентного состояния
    (join, этап 18) + локальные службы из назначений.

    ``assignments`` — эффективный набор (TOML ∪ персистентное состояние
    ноды), не только `settings.node.assignments` — см. `node/state.py`.

    ``on_peer_event``/``on_local_event`` — РАЗНЫЕ колбэки (живая находка
    2026-07-24, живой баг на проде: `task_result` от службы tasks молча
    терялся). Локальная служба репортует СВОЙ ЖЕ узел в `describe().info.
    node` (см. proto/server.py::broadcast_event — `src=Address(node=info.
    node,...)`), который для локальной службы равен `node_id` этой самой
    ноды — эхо-проверка в `_relay_peer_event` (`env.src.node == node_id`)
    расценивала это как "моё же событие вернулось от соседа" и молча
    дропала. Для событий ПИРОВ (`src.node` — чужой узел) проверка верна, для
    ЛОКАЛЬНЫХ служб — никогда не эхо (локальная связь однонаправленная,
    возврата по кругу физически нет), проверять нечего."""
    peer_configs: dict[str, SwarmNodeConfig] = {n.id: n for n in settings.swarm.nodes}
    for p in extra_peers:
        peer_configs.setdefault(p.id, p)
    peers = {
        pid: PeerLink(pid, cfg.endpoint, token=settings.swarm.token, on_event=on_peer_event)
        for pid, cfg in peer_configs.items()
        if pid != node_id  # свой id в списке — не пир
    }
    # Маршрут строится по ИМЕНИ СЛУЖБЫ, а не по строке назначения целиком:
    # "telegram-bot@alfred:standby" — это всё та же служба telegram-bot.
    assigned_services = {a.service for a in _safe_parse_all(assignments)}
    local = {
        # on_event=on_local_event (НЕ on_peer_event, см. докстринг выше) —
        # локальные службы (llm — llm_idle_sleep, tasks — task_result и
        # т.п.) тоже могут эмитить события, которые нужно довезти до бота
        # через ту же ретрансляцию, что уже работает для событий пиров.
        name: PeerLink(name, endpoint, token=settings.swarm.token, on_event=on_local_event)
        for name, endpoint in local_service_endpoints(settings).items()
        if name in assigned_services
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
    is_local: bool = False,
    replicator: ConfigReplicator | None = None,
) -> None:
    """Ретрансляция события (пира или локальной службы) своим клиентам +
    замыкание сетки (этап 18) для событий пиров.

    Эхо-проверка (``env.src.node == node_id`` — "моё же событие вернулось
    от соседа") применяется ТОЛЬКО к событиям пиров (``is_local=False``):
    локальная служба репортует src.node == node_id этой же ноды не потому,
    что это эхо, а потому что это буквально её собственный узел — для
    локальной связи возврата по кругу не бывает (см. build_router). Живой
    баг на проде 2026-07-24: без этого различения `task_result` от службы
    tasks молча терялся здесь же, до бота дело не доходило.

    ``node_joined`` от уже связанного пира — авто-подключиться к новому
    узлу тем же путём, каким мы сами узнаём о событиях: так третий узел, не
    участвовавший в handshake напрямую, всё равно достраивает полную сетку
    за один хоп репликации события, без отдельного каталога. Локальная
    служба этот тип события никогда не эмитит — веткой ниже не задевается.

    ``seen`` — дедуп по ``env.id``: в связном рое (≥3 узла, каждый с каждым)
    одно событие приходит несколькими путями, а без дедупа каждый узел
    ретранслирует КАЖДУЮ полученную копию заново — лавина (см. SeenEvents).
    """
    if not is_local and env.src is not None and env.src.node == node_id:
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
    if (
        replicator is not None
        and env.type == MSG_EVENT
        and env.payload.get("event") == EVENT_INSTANCE_CONFIG_CHANGED
    ):
        # Сосед объявил новую ревизию пакета — забрать её, если этот инстанс
        # назначен и нам (node/replication.py).
        await replicator.on_config_changed(env)
    if server is not None:
        await server.broadcast_envelope(env)


async def run_node(settings: Settings, config_path: str | None = None) -> bool:
    """Запустить ноду; вернуть True, если перед выходом запрошен само-рестарт
    (см. `restart_node` — cli.main() тогда делает os.execv на том же PID)."""
    # Сервер создаётся до супервизора: emit замыкается на его broadcast.
    server: ProtoServer | None = None
    replicator: ConfigReplicator | None = None
    lease: LeaseManager | None = None
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
            replicator=replicator,
        )

    async def on_local_event(env: Envelope) -> None:
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
            is_local=True,
        )

    # assignments в TOML — стартовый набор, не единственный источник:
    # состояние из assign/unassign в рантайме (nodectl/бот) переживает
    # рестарт через state_path (node/state.py), а не только через TOML.
    # То же для пиров: [[swarm.nodes]] — стартовый список, join (этап 18)
    # добавляет к нему динамически.
    state = NodeState.load(settings.node.state_path)
    effective_assignments = sorted(set(settings.node.assignments) | set(state.assignments))

    def on_fenced(slot_name: str) -> None:
        # Аренда создаётся ниже супервизора (ей нужен router), поэтому
        # колбэк смотрит на переменную, а не захватывает объект.
        if lease is not None:
            lease.note_fenced(slot_name)

    supervisor = Supervisor(
        effective_assignments,
        config_path,
        emit=emit,
        restart_delay_s=settings.node.restart_delay_s,
        stop_timeout_s=settings.node.stop_timeout_s,
        on_fenced=on_fenced,
    )
    if not supervisor.services:
        log.warning("Нет ни одного валидного назначения — нода работает вхолостую")

    router = build_router(
        settings, node_id, effective_assignments, state.peers, on_peer_event, on_local_event
    )
    # Пакеты настроек лежат рядом с config.toml; без файлового конфига их
    # попросту негде держать — тогда репликации нет (и она не нужна).
    packages_dir = instances_dir(config_path)
    replicator = (
        ConfigReplicator(
            node_id,
            InstanceStore(packages_dir, node_id),
            supervisor=supervisor,
            router=router,
            emit=emit,
        )
        if packages_dir is not None
        else None
    )
    lease = LeaseManager(
        node_id,
        settings.node.kind,
        supervisor,
        router=router,
        emit=emit,
        grace_s=settings.swarm.failover_grace_s,
    )
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
        node_kind=settings.node.kind,
        replicator=replicator,
        lease=lease,
    )
    server = ProtoServer(endpoints, node_service, token=settings.swarm.token, router=router.route)
    await server.start()
    for link in (*router.peers.values(), *router.local_services.values()):
        await link.start()
    # До запуска служб: пакет, правленный пока нода была выключена, должен
    # вступить в силу сразу, а не после лишнего рестарта службы.
    if replicator is not None:
        await replicator.start()
    await supervisor.start_all()
    # Синглтоны стартует аренда, а не start_all: даже активная роль сначала
    # убеждается, что службу не держит другая нода (node/lease.py).
    await lease.start()

    # Присутствие соседей: пропажа ноды, обязанной быть в сети, — авария;
    # пропажа рабочей станции — норма (node/watch.py).
    watcher = PresenceWatcher(
        node_id,
        router,
        emit=emit,
        state=state,
        state_path=settings.node.state_path,
        down_after_s=settings.swarm.node_down_alert_after_s,
    )
    await watcher.start()

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
        await watcher.stop()
        await lease.stop()
        if replicator is not None:
            await replicator.stop()
        await supervisor.stop_all()
        for link in (*router.peers.values(), *router.local_services.values()):
            await link.stop()
        await server.stop()
        log.info("Нода остановлена чисто")
    return restart_requested
