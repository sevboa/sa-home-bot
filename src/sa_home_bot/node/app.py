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

import asyncio
import dataclasses
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence

from sa_home_bot import __version__
from sa_home_bot.config import Settings, SwarmNodeConfig
from sa_home_bot.node import assignments as assignments_mod
from sa_home_bot.node import update as node_update
from sa_home_bot.node.discovery import SwarmDiscovery
from sa_home_bot.node.instances import InstanceStore, instances_dir
from sa_home_bot.node.lease import LeaseManager
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.replication import (
    EVENT_INSTANCE_CONFIG_CHANGED,
    ConfigReplicator,
)
from sa_home_bot.node.service import EVENT_NODE_JOINED, EVENT_RESTART_APPLIED, NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.node.watch import EVENT_NODE_LEAVING, PresenceWatcher
from sa_home_bot.proto.messages import MSG_EVENT, Envelope, ProtoError
from sa_home_bot.proto.server import ProtoServer
from sa_home_bot.services import registry
from sa_home_bot.utils.lifespan import Lifespan
from sa_home_bot.utils.shutdown import STEP_TIMEOUT_S, ShutdownBudget, arm_hard_exit

log = logging.getLogger(__name__)

# Сколько времени даётся всему останову СВЕРХ срока на гашение дочерних служб
# (`[node].stop_timeout_s`): прощание с роем, фоновые задачи, линки, сокеты.
# Двадцати секунд хватает рою из десятка нод даже при полностью мёртвой сети.
SHUTDOWN_BUDGET_S = 20.0

# Запас сторожу поверх бюджета: сначала свои потолки и честная запись в лог,
# и только потом — принудительный выход.
HARD_EXIT_MARGIN_S = 15.0

# Фабрика линка: (имя, endpoint или список адресов) → PeerLink со всеми
# обязательными колбэками.
LinkFactory = Callable[[str, "str | Sequence[str]"], PeerLink]

# Локальные службы со своим proto-сервером, к которым нода умеет
# проксировать (telegram-bot — клиент, своего сервера у него нет).
# Состав — из реестра служб (services/registry.py), а не из списка здесь.
def local_service_endpoints(settings: Settings) -> dict[str, str]:
    return registry.service_endpoints(settings)


# Размер дедуп-набора: изначально 512 «событий мало», но с тех пор через тот
# же канал ходят tool_call/task_result/llm_idle_sleep/алерты монитора — при
# всплеске больше maxsize РАЗНЫХ id за время оборота циркулирующей копии
# старый id вытесняется и событие принимается повторно, т.е. защита протекает
# ровно в штормовом сценарии. 2048 строк-ключей — копейки по памяти.
_MAX_SEEN_EVENTS = 2048

# Второй предохранитель от шторма ретрансляции, НЕЗАВИСИМЫЙ от дедупа
# (ARCHITECTURE §11: «TTL/hop-limit в конверте — предохранитель на случай
# рассинхрона»). Дедуп по id — единственный барьер, и он дырявый по краям:
# рестарт ноды сбрасывает набор, переполнение вытесняет старые id. Диаметр
# полного графа — 1 хоп, будущего Chord — O(log n): 8 хватает с запасом,
# а лавине — нет.
MAX_EVENT_HOPS = 8


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


def make_link_factories(
    settings: Settings, node_id: str, on_peer_event, on_local_event, on_endpoints=None
) -> tuple[LinkFactory, LinkFactory]:
    """Единственный источник правды о том, КАК создавать линки.

    Живой баг (найден аудитом 2026-07-28): линки, созданные в рантайме
    (`join`/`swarm_join`/`assign` в node/service.py), собирались на месте
    голым `PeerLink(id, endpoint, token=...)` — без `on_event` (события
    соседа/службы молча терялись до рестарта ноды — тот же класс бага, что
    уже чинил 00f9e00 для локальных служб) и без `self_node` (в auth не
    ехали имя и инкарнация — уборка протухших линков после ребута соседа,
    proto/server.py::_note_peer, для них не работала). Фабрики закрывают
    класс бага целиком: все шесть точек создания используют их.

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

    def make_peer_link(peer_id: str, endpoint: str | Sequence[str]) -> PeerLink:
        return PeerLink(
            peer_id,
            endpoint,
            token=settings.swarm.token,
            on_event=on_peer_event,
            # Адреса, которые сосед назовёт в hello (этап 24), сохраняются в
            # состоянии ноды — иначе выученный LAN-путь терялся бы при каждом
            # рестарте и рой снова начинал бы с одного адреса из конфига.
            on_endpoints=on_endpoints,
            # Представляемся соседу в auth — по этому имени он уронит свой
            # протухший линк к нам, когда мы переподключимся после ребута
            # (proto/server.py::_note_peer). Локальным службам ниже незачем:
            # они живут в том же процессе-родителе, «переподключения после
            # ребута соседа» там не бывает.
            self_node=node_id,
        )

    def make_local_link(service: str, endpoint: str | Sequence[str]) -> PeerLink:
        # on_event=on_local_event (НЕ on_peer_event, см. докстринг выше) —
        # локальные службы (llm — llm_idle_sleep, tasks — task_result и
        # т.п.) тоже могут эмитить события, которые нужно довезти до бота
        # через ту же ретрансляцию, что уже работает для событий пиров.
        return PeerLink(service, endpoint, token=settings.swarm.token, on_event=on_local_event)

    return make_peer_link, make_local_link


def peer_candidates(
    nodes: Sequence[SwarmNodeConfig], extra: Sequence[SwarmNodeConfig]
) -> dict[str, list[str]]:
    """Адреса каждого пира из обоих источников, в порядке проб (этап 24).

    Состояние ноды идёт ПЕРВЫМ, а не как раньше (TOML побеждал целиком):
    там лежит последний удачный путь и всё, что сосед о себе рассказал, —
    то есть более свежая правда, чем один адрес, вписанный в конфиг руками.
    Сам конфиг при этом не теряется: он добавляется следом и остаётся
    единственным, что работает, когда состояние пустое (первый запуск).
    """
    out: dict[str, list[str]] = {}
    for cfg in (*extra, *nodes):
        known = out.setdefault(cfg.id, [])
        for value in (cfg.endpoint, *cfg.endpoints):
            if value and value not in known:
                known.append(value)
    return out


def build_router(
    settings: Settings,
    node_id: str,
    assignments: list[str],
    extra_peers: list[SwarmNodeConfig],
    make_peer_link: LinkFactory,
    make_local_link: LinkFactory,
) -> NodeRouter:
    """Маршрутизатор: пиры из [[swarm.nodes]] ∪ персистентного состояния
    (join, этап 18) + локальные службы из назначений.

    ``assignments`` — эффективный набор (TOML ∪ персистентное состояние
    ноды), не только `settings.node.assignments` — см. `node/state.py`.

    Линки создаются только фабриками (см. make_link_factories)."""
    peers = {
        pid: make_peer_link(pid, candidates)
        for pid, candidates in peer_candidates(settings.swarm.nodes, extra_peers).items()
        if pid != node_id  # свой id в списке — не пир
    }
    # Маршрут строится по ИМЕНИ СЛУЖБЫ, а не по строке назначения целиком:
    # "telegram-bot@alfred:standby" — это всё та же служба telegram-bot.
    assigned_services = {a.service for a in _safe_parse_all(assignments)}
    local = {
        name: make_local_link(name, endpoint)
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


def _remember_peer(
    state: NodeState, node_id: str, endpoint: str, endpoints: Sequence[str] = ()
) -> None:
    """Персистентный справочник пиров: id + последний удачный адрес + все
    остальные известные пути к нему (этап 24), не полный конфиг.

    Тип соседа (его пишет PresenceWatcher, node/watch.py) при перезаписи
    сохраняется: он узнаётся из hello и нужен именно тогда, когда соседа уже
    не спросить — потерять его на обновлении адресов значило бы разучиться
    отличать «сервер пропал» от «рабочая станция уснула».
    """
    known = next((p for p in state.peers if p.id == node_id), None)
    others = [p for p in state.peers if p.id != node_id]
    rest = [e for e in endpoints if e != endpoint]
    state.peers = [
        *others,
        SwarmNodeConfig(
            id=node_id,
            endpoint=endpoint,
            endpoints=rest,
            kind=known.kind if known is not None else "",
        ),
    ]


async def _announce_version_if_changed(
    state: NodeState,
    state_path: str,
    emit: Callable[[str, dict], Awaitable[None]],
) -> None:
    """Событие ``restart_applied``, если версия сменилась с прошлого старта.

    Молчит на самом первом старте с этим полем (``last_known_version`` ещё
    ``None`` — иначе разом «применили обновление» получил бы весь рой сразу
    после деплоя, где его в реальности не было) и когда версия не менялась.
    Срабатывает и на обычном `restart_node`/`update`, и на ручном `nodectl
    update` по SSH, и на голом передеплое юнита — в любом случае это
    единственный источник правды «нода реально ИСПОЛНЯЕТ новую версию», не
    просто положила файлы на диск (для этого уже есть update_finished,
    node/service.py::_update).
    """
    if state.last_known_version is not None and state.last_known_version != __version__:
        await emit(
            EVENT_RESTART_APPLIED,
            {"from": state.last_known_version, "to": __version__},
        )
    if state.last_known_version != __version__:
        state.last_known_version = __version__
        state.save(state_path)


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
    """Ретрансляция события (пира или локальной службы) своим клиентам +
    замыкание сетки (этап 18) для событий пиров.

    Эхо-проверка (``env.src.node == node_id`` — "моё же событие вернулось
    от соседа") применяется ТОЛЬКО к событиям пиров (``is_local=False``):
    локальная служба репортует src.node == node_id этой же ноды не потому,
    что это эхо, а потому что это буквально её собственный узел — для
    локальной связи возврата по кругу не бывает (см. make_link_factories).
    Живой баг на проде 2026-07-24: без этого различения `task_result` от
    службы tasks молча терялся здесь же, до бота дело не доходило.

    ``node_joined`` от уже связанного пира — авто-подключиться к новому
    узлу тем же путём, каким мы сами узнаём о событиях: так третий узел, не
    участвовавший в handshake напрямую, всё равно достраивает полную сетку
    за один хоп репликации события, без отдельного каталога. Локальная
    служба этот тип события никогда не эмитит — веткой ниже не задевается.

    ``seen`` — дедуп по ``env.id``: в связном рое (≥3 узла, каждый с каждым)
    одно событие приходит несколькими путями, а без дедупа каждый узел
    ретранслирует КАЖДУЮ полученную копию заново — лавина (см. SeenEvents).
    Второй, независимый рубеж — счётчик ретрансляций в конверте: событие,
    пережившее MAX_EVENT_HOPS пересылок, уже точно циркулирует по кругу
    (диаметр полного графа — 1), дальше не идёт.
    """
    if not is_local and env.src is not None and env.src.node == node_id:
        return
    if seen.seen(env.id):
        return
    if env.hops >= MAX_EVENT_HOPS:
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
            # Новый узел назвал все свои адреса (этап 24) — связываемся по
            # лучшему из них, а не по одному, попавшему в событие.
            raw = data.get("endpoints")
            candidates = [new_endpoint]
            if isinstance(raw, list):
                candidates += [e for e in raw if isinstance(e, str) and e and e != new_endpoint]
            link = make_peer_link(new_id, candidates)
            await router.add_peer(link)
            _remember_peer(state, new_id, new_endpoint, candidates)
            state.save(state_path)
            log.info("Рой: авто-подключение к %s (%s) по node_joined", new_id, candidates)
    if (
        replicator is not None
        and env.type == MSG_EVENT
        and env.payload.get("event") == EVENT_INSTANCE_CONFIG_CHANGED
    ):
        # Сосед объявил новую ревизию пакета — забрать её, если этот инстанс
        # назначен и нам (node/replication.py).
        await replicator.on_config_changed(env)
    if server is not None:
        await server.broadcast_envelope(dataclasses.replace(env, hops=env.hops + 1))
    if env.type == MSG_EVENT and env.payload.get("event") == EVENT_NODE_LEAVING:
        # Сосед прощается перед остановкой (этап 23): роняем линк к нему и
        # помечаем уход штатным — точный детект вместо heartbeat и без
        # ложного node_down через down_after_s. СТРОГО после ретрансляции:
        # note_left рвёт то самое соединение, по которому событие пришло, —
        # отмена читающей задачи (мы выполняемся в её callback'е) прилетела
        # бы на ближайшем await и оборвала бы рассылку боту.
        leaving = env.payload.get("data", {}).get("node") or (env.src.node if env.src else None)
        left_link = router.peers.get(leaving) if leaving else None
        if left_link is not None:
            left_link.note_left()


async def run_node(settings: Settings, config_path: str | None = None) -> bool:
    """Запустить ноду; вернуть True, если перед выходом запрошен само-рестарт
    (см. `restart_node` — cli.main() тогда делает os.execv на том же PID)."""
    # Сервер создаётся до супервизора: emit замыкается на его broadcast.
    server: ProtoServer | None = None
    replicator: ConfigReplicator | None = None
    lease: LeaseManager | None = None
    # Читается в on_local_event ниже, назначается только после создания
    # (тот же приём, что и server выше) — сама NodeService нужна там, чтобы
    # среагировать на llm_went_idle своей же ноды (автовыключение по
    # простою, node/service.py::maybe_auto_poweroff_idle).
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
            make_peer_link=make_peer_link,
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
            make_peer_link=make_peer_link,
            server=server,
            seen=seen_events,
            is_local=True,
        )
        # Строковый литерал, не импорт из llm/service.py — та же конвенция,
        # что и в bot/node_events.py::EVENT_LLM_IDLE_SLEEP. `llm_went_idle`
        # (не `llm_idle_sleep`!) — эмитится ТОЛЬКО из _maybe_sleep_idle
        # (натуральный тайм-аут простоя), безусловно, даже без активных
        # chat_id (в отличие от llm_idle_sleep, который адресован реальным
        # чатам и молчит, если чатов не было — живая находка 2026-08-03: с
        # llm_idle_sleep автовыключение не срабатывало на машине, с которой
        # Alfred просто ни разу не заговорили). Ручной роспуск Alfred
        # (llm/service.py::_sleep_now(quiet=True)) не эмитит НИ ОДНО из двух
        # событий — машину сам по себе не гасит.
        if env.payload.get("event") == "llm_went_idle" and node_service is not None:
            asyncio.create_task(
                node_service.maybe_auto_poweroff_idle(), name="idle-poweroff-check"
            )

    def on_peer_endpoints(peer_id: str, endpoints: list[str]) -> None:
        """Линк узнал новые адреса соседа или переехал на другой путь —
        сохранить, чтобы не переучиваться после каждого рестарта."""
        if not endpoints:
            return
        _remember_peer(state, peer_id, endpoints[0], endpoints)
        state.save(settings.node.state_path)

    make_peer_link, make_local_link = make_link_factories(
        settings, node_id, on_peer_event, on_local_event, on_peer_endpoints
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
        settings, node_id, effective_assignments, state.peers, make_peer_link, make_local_link
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
    # Нода слушает socket (локальные фронтенды) и все адреса из listen —
    # TCP для пиров роя. Адрес, которого ещё нет (tailscale поднимается
    # десятки секунд), старт не задерживает: сервер доберёт его фоном.
    endpoints = [settings.node.socket, *settings.node.listen]
    node_service = NodeService(
        supervisor,
        router,
        node_id=node_id,
        restart_node=request_restart,
        state=state,
        state_path=settings.node.state_path,
        local_service_endpoints=local_service_endpoints(settings),
        swarm_token=settings.swarm.token,
        # Настроенный адрес объявляет умения join/swarm_join (он статичен и
        # известен сразу), а рою нода называет то, что РЕАЛЬНО слушает —
        # список меняется, пока адреса доезжают (см. ProtoServer.start).
        own_endpoint=settings.node.listen[0] if settings.node.listen else "",
        advertise=lambda: server.advertised_endpoints() if server is not None else [],
        emit=emit,
        # Дешёвая файловая проверка (без сети) — editable/не-git установка
        # (dev-чекаут) или win32 дают None, и check_update/update не
        # объявляются (см. update_source_for_this_platform).
        update_source=update_source_for_this_platform(),
        node_kind=settings.node.kind,
        power_controllable=settings.node.power_controllable,
        idle_poweroff=settings.node.idle_poweroff,
        replicator=replicator,
        lease=lease,
        # Динамические линки (join/assign в рантайме) обязаны собираться теми
        # же фабриками, что и стартовые, — см. make_link_factories.
        make_peer_link=make_peer_link,
        make_local_link=make_local_link,
    )
    def on_peer_connect(peer_node: str) -> None:
        """Сосед постучался к нам заново — значит, он перезапустился, и наш
        исходящий линк к нему держит труп прошлого соединения. Роняем его
        сразу, не дожидаясь ни heartbeat'а, ни ОС-таймаутов."""
        link = router.peers.get(peer_node)
        if link is not None:
            link.reconnect_now("сосед подключился заново")

    # Сигналы перехватываем ДО первого долгого шага, а не после того, как всё
    # поднялось (живая находка 2026-07-28): `server.start()` ждёт появления
    # своего адреса до трёх минут (Tailscale поднимается не мгновенно), и всё
    # это время остановка обрабатывалась бы по умолчанию — процесс умирал бы
    # на месте, не погасив уже запущенные дочерние службы.
    lifespan.install_signal_handlers()

    server = ProtoServer(
        endpoints,
        node_service,
        token=settings.swarm.token,
        router=router.route,
        on_peer_connect=on_peer_connect,
    )
    await server.start()
    # После server.start() — emit уже реально рассылает (см. определение
    # emit выше: broadcast_event молчит, пока server is None).
    await _announce_version_if_changed(state, settings.node.state_path, emit)
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

    # Маячок в локальной сети (этап 31): соседа в той же локалке не нужно
    # вписывать в конфиг, а уехавший DHCP-адрес чинится сам. Собирается
    # последним — ему нужны и join сервиса ноды, и реальные адреса сервера.
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
        # Каждый шаг — под потолком, вся последовательность — под общим
        # бюджетом (этап 29: на jeeves останов не заканчивался никогда, и по
        # логу нельзя было понять, на чём именно он встал). Детям бюджет
        # отдельный: погасить службу — честная работа, а не зависание, и
        # укладываться она обязана в свой stop_timeout_s.
        budget = ShutdownBudget(total_s=settings.node.stop_timeout_s + SHUTDOWN_BUDGET_S)
        # Последний рубеж — за нашим кодом: даже пройдя все шаги, процесс
        # может застрять в уборке задач внутри asyncio.run().
        arm_hard_exit(budget.total_s + HARD_EXIT_MARGIN_S)
        # Прощаемся ПЕРВЫМ делом, пока соединения живы: соседи по node_leaving
        # мгновенно и точно узнают о штатном уходе (этап 23) — без этого
        # «корректно остановилась» и «выдернули из розетки» неотличимы.
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
