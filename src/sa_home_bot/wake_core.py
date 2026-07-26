"""Общее ядро Wake-on-LAN через рой: раньше жило только в bot/swarm_view.py
и bot/handlers/wake.py (используется /wake и молчаливым прогревом перед
/ai), но оба этих модуля тянут aiogram на весь файл. Служба tasks (без
Telegram, живая находка 2026-07-24 при генерализации напоминаний в
отдельный сервис) тоже должна уметь будить спящую цель перед сроком
задачи — этот модуль ей это даёт без лишних зависимостей.

bot/swarm_view.py и bot/handlers/wake.py делегируют сюда (тонкие
реэкспорты), не дублируют логику — поведение /wake и /swarm не менялось.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.wake_state import cached as cached_wake_info
from sa_home_bot.bot.wake_state import remember as remember_wake_info
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, Address, ProtoError

log = logging.getLogger(__name__)

PEER_TIMEOUT_S = 3.0

# --- общие константы сценария presence/wake ---
#
# Раньше эти три жили копиями в bot/ai_flow.py и tasks/service.py, и правку
# 2b8fae7 (3 -> 6 с) пришлось делать в двух местах. Теперь источник один.

# Быстрая presence-проверка: только «отвечает ли вообще», не повод ждать
# столько же, сколько за настоящим ответом. Живая находка 2026-07-23: без
# явного укороченного таймаута get_state ждёт весь дефолт ProtoClient (10 с)
# на каждый хоп, а TCP-keepalive замечает пропавшего пира лишь за ~50 с.
# Живая находка 2026-07-25: 3 с ловило ложные «недоступна» на кратковременных
# подтормаживаниях winpc (WSL2/Ollama), не будучи реальным сном.
PRESENCE_TIMEOUT_S = 6.0

# Ожидание после magic packet. Живая находка 2026-07-27: 90 с было меньше
# собственного llm/ollama.py::_WARMUP_TIMEOUT_S (150 с) и меньше реального
# холодного старта winpc — нода успевала подняться уже ПОСЛЕ того, как рой
# признал её недоступной.
WAKE_POLL_TIMEOUT_S = 180.0
WAKE_POLL_INTERVAL_S = 3.0

# Прогрев самой службы (ACTION_WARMUP у llm) — не все службы такое
# объявляют, отсутствие поддержки (ERR_UNKNOWN_ACTION) не сбой.
WARMUP_TIMEOUT_S = 180.0

# Исходы ensure_service_ready.
READY = "ready"
UNREACHABLE = "unreachable"
WARMUP_FAILED = "warmup_failed"


@dataclass
class NodeReport:
    node_id: str
    alive: bool
    state: dict | None = None  # get_state сервиса node (None — не ответил)


async def fetch_state(
    node_link: ServiceLink, dst: Address | None, *, timeout_s: float = PEER_TIMEOUT_S
) -> dict | None:
    """Состояние службы или None, если не ответила за ``timeout_s``.

    Дефолт (PEER_TIMEOUT_S) — для обзорных опросов роя (/swarm, поиск
    LAN-waker), где нода либо рядом, либо её и не ждут. Сценарий presence
    перед запросом к модели передаёт PRESENCE_TIMEOUT_S: там цена ложного
    «недоступна» выше (лишний WoL посреди живого диалога).
    """
    try:
        return await asyncio.wait_for(node_link.get_state(dst=dst), timeout_s)
    except (ServiceUnavailableError, ProtoError, TimeoutError):
        return None


async def collect_reports(node_link: ServiceLink, own_state: dict) -> list[NodeReport]:
    """Параллельный сбор состояний всех нод роя (своя — первой). Минимальная
    версия для нужд wake — без монитора (см. bot/swarm_view.py::_collect
    для полной сводки /swarm, которая дополнительно тянет monitor.get_state
    на каждую ноду)."""
    own = NodeReport(node_id=own_state.get("node", "?"), alive=True, state=own_state)
    reports = [own]
    for peer in own_state.get("peers", []):
        pid = peer.get("id", "?")
        reports.append(NodeReport(node_id=pid, alive=bool(peer.get("alive"))))

    async def fill(report: NodeReport) -> None:
        if report.state is None:
            dst = Address(node=report.node_id, service="node")
            report.state = await fetch_state(node_link, dst)

    await asyncio.gather(*(fill(r) for r in reports if r.alive))
    return reports


async def _remember_all(store: Store, reports: list[NodeReport]) -> None:
    for r in reports:
        if r.state is not None:
            await remember_wake_info(store, r.node_id, r.state.get("wake"))


async def find_lan_waker(
    node_link: ServiceLink, store: Store, target_node_id: str, target_broadcast: str
) -> str | None:
    """Живая нода в том же сегменте LAN, что и уснувшая ``target_node_id``
    (совпадает объявленный ею broadcast) — она отправит magic packet вместо
    того, кто зовёт (бот или служба tasks вполне может крутиться вне этой
    локалки). Заодно свежит кэш wake-реквизитов всех увиденных сейчас нод
    (в СВОЁМ store вызывающего — у бота и у tasks кэши независимые, см.
    докстринг модуля)."""
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None
    reports = await collect_reports(node_link, own_state)
    await _remember_all(store, reports)
    for r in reports:
        if not r.alive or r.node_id == target_node_id or r.state is None:
            continue
        candidate = r.state.get("wake")
        if candidate and candidate.get("broadcast") == target_broadcast:
            return r.node_id
    return None


async def wait_for_service(
    node_link: ServiceLink,
    node_id: str,
    service: str,
    timeout_s: float,
    interval_s: float = 3.0,
) -> bool:
    """Опрашивать get_state удалённой службы, пока не ответит или не истечёт
    ``timeout_s`` — дождаться, пока цель снова окажется на связи после WoL."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    dst = Address(node=node_id, service=service)
    while True:
        if await fetch_state(node_link, dst) is not None:
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(interval_s)


async def resolve_wake_info(
    node_link: ServiceLink, store: Store, node_id: str
) -> dict[str, str] | None:
    """Реквизиты WoL спящей ноды: сначала свой кэш, потом — рой.

    Живая находка 2026-07-27 (инцидент 19:34): раньше тут стоял голый
    ``cached_wake_info``, а наполнялся кэш ТОЛЬКО в ``find_lan_waker``, куда
    ``wake_swarm_node_core`` доходит лишь после успешного чтения кэша.
    Курица-яйцо: у службы, которая не опрашивает рой сама (tasks), кэш
    оставался пустым навсегда — WoL не уходил НИ РАЗУ, задача молча падала
    в «цель недоступна». Теперь при промахе спрашиваем свою локальную ноду:
    она помнит реквизиты соседей с их hello и после того, как те уснули
    (node/peers.py::PeerLink.wake_info), — и сразу кладём в кэш, чтобы
    пережить и рестарт самого роя.
    """
    info = await cached_wake_info(store, node_id)
    if info is not None:
        return info
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None
    for peer in own_state.get("peers", []):
        if peer.get("id") != node_id:
            continue
        await remember_wake_info(store, node_id, peer.get("wake"))
        return await cached_wake_info(store, node_id)
    return None


async def try_warmup(node_link: ServiceLink, dst: Address) -> bool:
    """Лучшее из возможного: дёргаем условное действие ``warmup`` у цели
    (llm/service.py его объявляет) — отсутствие поддержки
    (ERR_UNKNOWN_ACTION) не ошибка, просто у этой службы нет отдельного
    прогрева (раз dst и так отвечает, этого достаточно)."""
    try:
        await node_link.command("warmup", {}, dst=dst, timeout=WARMUP_TIMEOUT_S)
        return True
    except ProtoError as exc:
        return exc.code == ERR_UNKNOWN_ACTION
    except (ServiceUnavailableError, TimeoutError):
        return False


async def ensure_service_ready(
    node_link: ServiceLink,
    store: Store,
    node_id: str,
    service: str,
    *,
    wake_timeout_s: float | None = None,
) -> str:
    """Довести службу на (возможно спящей) ноде до готовности отвечать.

    Один сценарий на всех: presence -> при необходимости WoL и ожидание
    подъёма -> прогрев. Раньше он был руками продублирован в bot/ai_flow.py
    и tasks/service.py с расходящимися константами.

    Возвращает ``READY`` / ``UNREACHABLE`` (машину не удалось поднять) /
    ``WARMUP_FAILED`` (нода на связи, но служба не прогрелась).
    """
    if wake_timeout_s is None:
        wake_timeout_s = WAKE_POLL_TIMEOUT_S
    dst = Address(node=node_id, service=service)
    state = await fetch_state(node_link, dst, timeout_s=PRESENCE_TIMEOUT_S)
    if state is not None and not state.get("asleep"):
        return READY

    if state is None:
        outcome = await wake_swarm_node_core(node_link, store, node_id)
        if not outcome.ok:
            log.warning("wake: не удалось разбудить «%s»: %s", node_id, outcome.detail)
            return UNREACHABLE
        log.info("wake: «%s» разбужена, ждём %s до %.0f с", node_id, service, wake_timeout_s)
        if not await wait_for_service(
            node_link, node_id, service, wake_timeout_s, WAKE_POLL_INTERVAL_S
        ):
            log.warning(
                "wake: «%s» не подняла службу %s за %.0f с", node_id, service, wake_timeout_s
            )
            return UNREACHABLE

    if await try_warmup(node_link, dst):
        return READY
    log.warning("wake: прогрев %s/%s не удался", node_id, service)
    return WARMUP_FAILED


@dataclass(frozen=True)
class WakeOutcome:
    """``detail`` — уже готовый HTML-текст для чата (эмодзи, как в /wake),
    чтобы бот мог переслать его без изменения поведения; служба tasks его
    просто не показывает никому (использует только ``ok``)."""

    ok: bool
    detail: str


async def wake_swarm_node_core(node_link: ServiceLink, store: Store, node_id: str) -> WakeOutcome:
    """Будим известную ноду по её кэшированным реквизитам — отправляет не
    сам вызывающий, а живая нода из той же LAN (см. докстринг модуля).
    Переиспользуется /wake, молчаливым прогревом перед /ai (bot/ai_flow.py)
    и прогревом задач (tasks/service.py)."""
    info = await resolve_wake_info(node_link, store, node_id)
    if info is None:
        return WakeOutcome(
            False, f"⚙️ Нет данных о MAC «{node_id}» — нода ещё ни разу не была видна в рое."
        )

    waker = await find_lan_waker(node_link, store, node_id, info["broadcast"])
    if waker is None:
        return WakeOutcome(
            False, f"⚠️ Некому отправить сигнал: нет живой ноды в той же сети, что «{node_id}»."
        )

    dst = Address(node=waker, service="node")
    try:
        await node_link.command("send_wol", {"mac": info["mac"]}, dst=dst)
    except ServiceUnavailableError:
        return WakeOutcome(False, f"⚠️ Нода «{waker}» перестала отвечать во время отправки.")
    except ProtoError as exc:
        return WakeOutcome(False, f"❌ {waker}: {exc.message}")

    return WakeOutcome(
        True,
        f"🔌 Magic packet для «{node_id}» (<code>{info['mac']}</code>) отправлен через "
        f"ноду «{waker}». Появится в /nodes, как поднимется.",
    )
