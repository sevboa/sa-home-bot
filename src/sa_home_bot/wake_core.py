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
from collections.abc import Awaitable, Callable
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
#
# Живой сбой 2026-07-30: было 180 с — меньше, чем реальный холодный старт
# модели. Замер на winpc: Qwen3.6-35B-A3B поднимается с диска 201 с
# (`load_duration` из ответа Ollama), то есть первый прогрев после долгого
# простоя был обречён независимо от того, здоров бэкенд или нет. Значение
# согласовано с `[llm].request_timeout_s` (360 с): дольше ждать бессмысленно —
# столько же ждёт и обычный запрос. Вызывающий может задать своё
# (`ensure_service_ready(warmup_timeout_s=...)`) — железо у нод разное.
WARMUP_TIMEOUT_S = 360.0

# Исходы ensure_service_ready.
READY = "ready"
UNREACHABLE = "unreachable"
WARMUP_FAILED = "warmup_failed"
# Живая находка при аудите механизма пробуждения (2026-08-26): раньше
# "не отвечает" был один исход на два разных случая — "машина спит" (WoL
# поможет) и "машина жива, но именно эта служба упала/зависла" (WoL
# бесполезен, magic packet некому будить). ensure_service_ready теперь
# различает их сам (см. ниже) через дешёвую доп.проверку присутствия ноды
# (служба NODE_SERVICE) ПЕРЕД тем, как посылать WoL.
CRASHED = "crashed"

# Фазы долгого ожидания внутри ensure_service_ready — передаются в
# необязательный ``on_phase_change`` колбэк ДО того, как начинается
# соответствующее ожидание (не после), чтобы вызывающий успел показать
# статус пользователю раньше, а не постфактум (та же причина, по которой
# bot/ai_flow.py::_announce_steps звался вручную перед wake_swarm_node_core
# до этого рефакторинга — см. Приложение план про живучесть статусов).
PHASE_WAKING = "waking"  # служба не отвечает вовсе — отправляем WoL и ждём
PHASE_WOKEN = "woken"  # WoL сработал, нода снова на связи (перед прогревом)
PHASE_WARMING = "warming"  # служба отвечает, но себя считает уснувшей —
# либо только что проснулась после WAKING — прогреваем модель


# Имена служб, к которым ходит обзорный сбор. Литералами, а не импортом из
# bot/node_view.py и bot/status_view.py — те тянут aiogram, чего этот модуль
# сознательно избегает (см. докстринг).
NODE_SERVICE = "node"
MONITOR_SERVICE = "monitor"


@dataclass
class NodeReport:
    node_id: str
    alive: bool
    state: dict | None = None  # get_state сервиса node (None — не ответил)
    monitor: dict | None = None  # get_state монитора (None — не ответил/нет)
    kind: str = ""  # тип машины (server|workstation|vps), см. node/kind.py
    left: bool = False  # нода предупредила о штатном уходе (node_leaving)


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


async def collect_reports(
    node_link: ServiceLink, own_state: dict, *, with_monitor: bool = False
) -> list[NodeReport]:
    """Параллельный сбор состояний всех нод роя (своя — первой).

    ``with_monitor`` — дополнительно опросить монитор каждой ноды. Нужен
    сводке /swarm и тулу swarm_status (здоровье/диски); сценариям wake — нет,
    там лишний запрос на каждую ноду ради данных, которые никто не прочтёт.

    Раньше версия с монитором жила отдельной копией в bot/swarm_view.py
    (``_collect``/``_NodeReport``) — слито сюда, когда за теми же данными
    пришёл тул swarm_status: bot/tools.py не может импортировать swarm_view,
    тот тянет aiogram, а служба tasks импортирует tools.
    """
    own = NodeReport(
        node_id=own_state.get("node", "?"),
        alive=True,
        state=own_state,
        kind=own_state.get("kind", ""),
    )
    reports = [own]
    for peer in own_state.get("peers", []):
        pid = peer.get("id", "?")
        reports.append(
            NodeReport(
                node_id=pid,
                alive=bool(peer.get("alive")),
                # Тип известен и для недоступной ноды — нода его помнит
                # (node/watch.py), иначе не отличить сон от аварии.
                kind=str(peer.get("kind") or ""),
                left=bool(peer.get("left")),
            )
        )

    async def fill(report: NodeReport) -> None:
        # Таймаут читается здесь, а не берётся дефолтом fetch_state: дефолт
        # аргумента вычисляется один раз при импорте, и подмена
        # PEER_TIMEOUT_S (тесты про зависшего пира) до него не доходила бы.
        if report.state is None:
            dst = Address(node=report.node_id, service=NODE_SERVICE)
            report.state = await fetch_state(node_link, dst, timeout_s=PEER_TIMEOUT_S)
        if with_monitor:
            report.monitor = await fetch_state(
                node_link,
                Address(node=report.node_id, service=MONITOR_SERVICE),
                timeout_s=PEER_TIMEOUT_S,
            )

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


async def try_warmup(
    node_link: ServiceLink, dst: Address, timeout_s: float = WARMUP_TIMEOUT_S
) -> bool:
    """Лучшее из возможного: дёргаем условное действие ``warmup`` у цели
    (llm/service.py его объявляет) — отсутствие поддержки
    (ERR_UNKNOWN_ACTION) не ошибка, просто у этой службы нет отдельного
    прогрева (раз dst и так отвечает, этого достаточно).

    Причина неудачи обязательно уходит в лог: без этого разбор сводился к
    «прогрев не удался» на одной машине и походу за логами службы на другой
    (живой сбой 2026-07-30 — на winpc это ещё и Windows-служба).
    """
    try:
        await node_link.command("warmup", {}, dst=dst, timeout=timeout_s)
        return True
    except ProtoError as exc:
        if exc.code == ERR_UNKNOWN_ACTION:
            return True
        log.warning(
            "wake: %s/%s отказал в прогреве: %s — %s", dst.node, dst.service, exc.code, exc.message
        )
        return False
    except (ServiceUnavailableError, TimeoutError) as exc:
        log.warning(
            "wake: %s/%s не ответил на прогрев за %.0f с (%s)",
            dst.node, dst.service, timeout_s, type(exc).__name__,
        )
        return False


async def ensure_service_ready(
    node_link: ServiceLink,
    store: Store,
    node_id: str,
    service: str,
    *,
    wake_timeout_s: float | None = None,
    wake_poll_interval_s: float = WAKE_POLL_INTERVAL_S,
    warmup_timeout_s: float = WARMUP_TIMEOUT_S,
    on_phase_change: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Довести службу на (возможно спящей) ноде до готовности отвечать.

    Один сценарий на всех: presence -> (если нужно) различить сон/аварию ->
    при необходимости WoL и ожидание подъёма -> прогрев, заземлённый на
    реальный ответ Ollama, а не на самоотчёт службы. Раньше был руками
    продублирован в bot/ai_flow.py (собственная, менее строгая версия) и
    tasks/service.py — единственная функция теперь используется отовсюду,
    включая bot/ai_flow.py::request_alfred (после аудита 2026-08-26,
    Приложения 2-4 плана механизма готовности LLM).

    Возвращает:
    - ``READY`` — служба готова принять запрос прямо сейчас;
    - ``UNREACHABLE`` — машину не удалось поднять (или не удалось поднять
      службу на ней за ``wake_timeout_s`` после WoL);
    - ``CRASHED`` — нода жива (ответила на presence NODE_SERVICE), но именно
      эта служба не отвечает вовсе: WoL здесь бесполезен, машину будить не
      нужно (см. CRASHED выше про живую находку 2026-08-26 — раньше это
      неотличимо смешивалось с UNREACHABLE, и код всё равно слал magic
      packet и ждал до ``wake_timeout_s`` машине, которая и не думала спать);
    - ``WARMUP_FAILED`` — служба на связи, но прогрев (заземление на Ollama)
      не подтверждён.

    ``on_phase_change`` — необязательный колбэк ``async def(phase: str)``,
    вызывается ДО начала соответствующего долгого ожидания (``PHASE_WAKING``/
    ``PHASE_WOKEN``/``PHASE_WARMING`` выше) — вызывающий может показать
    статус пользователю заранее, а не после того, как ожидание уже прошло.
    Не зовётся вовсе на "уже тёплом" пути (служба ответила и не считает себя
    спящей) — там заземляющий прогрев (см. ниже) ожидается быстрым, отдельный
    статус не нужен (сохраняет прежнее поведение вызывающих).

    ``wake_poll_interval_s`` — интервал опроса между попытками в
    ``wait_for_service`` (параметризован, а не жёстко ``WAKE_POLL_INTERVAL_S``,
    чтобы тесты могли ускорить поллинг, не трогая реальный дефолт).
    """
    if wake_timeout_s is None:
        wake_timeout_s = WAKE_POLL_TIMEOUT_S
    dst = Address(node=node_id, service=service)

    async def _finish_warmup() -> str:
        if await try_warmup(node_link, dst, warmup_timeout_s):
            return READY
        log.warning("wake: прогрев %s/%s не удался", node_id, service)
        return WARMUP_FAILED

    state = await fetch_state(node_link, dst, timeout_s=PRESENCE_TIMEOUT_S)

    if state is None:
        # Служба не отвечает вовсе — прежде чем слать WoL, дёшево проверяем,
        # жива ли сама нода (служба node): если да, будить нечего и незачем —
        # magic packet тут не поможет, а 180с поллинга только тянут время
        # зря (см. CRASHED выше).
        node_alive = service != NODE_SERVICE and (
            await fetch_state(
                node_link, Address(node=node_id, service=NODE_SERVICE), timeout_s=PRESENCE_TIMEOUT_S
            )
            is not None
        )
        if node_alive:
            log.warning(
                "wake: нода «%s» жива, но служба %s не отвечает — WoL тут бесполезен",
                node_id, service,
            )
            return CRASHED
        if on_phase_change is not None:
            await on_phase_change(PHASE_WAKING)
        outcome = await wake_swarm_node_core(node_link, store, node_id)
        if not outcome.ok:
            log.warning("wake: не удалось разбудить «%s»: %s", node_id, outcome.detail)
            return UNREACHABLE
        log.info("wake: «%s» разбужена, ждём %s до %.0f с", node_id, service, wake_timeout_s)
        if not await wait_for_service(
            node_link, node_id, service, wake_timeout_s, wake_poll_interval_s
        ):
            log.warning(
                "wake: «%s» не подняла службу %s за %.0f с", node_id, service, wake_timeout_s
            )
            return UNREACHABLE
        if on_phase_change is not None:
            await on_phase_change(PHASE_WOKEN)
            await on_phase_change(PHASE_WARMING)
        return await _finish_warmup()

    if state.get("asleep"):
        if on_phase_change is not None:
            await on_phase_change(PHASE_WARMING)
        return await _finish_warmup()

    # Служба сама отвечает "не сплю" — самоотчёт (self._asleep в
    # llm/service.py), не проверенный против Ollama напрямую на каждый
    # запрос (это добавило бы сетевой круг ожидания в самый частый, уже
    # тёплый путь). Заземление против рассинхрона после рестарта процесса
    # службы сделано у источника: llm/service.py инициализирует _asleep=True
    # при старте (а не False), так что именно ПЕРВЫЙ запрос после рестарта
    # честно пройдёт через ветку выше и настоящий прогрев — см. Приложение 3
    # плана про живую находку 2026-08-26. Внешнее выгружение модели самой
    # Ollama (не через _sleep_now службы) сюда не заземлено и остаётся
    # известным, задокументированным пробелом — не решаем его ценой сетевого
    # вызова на КАЖДЫЙ запрос.
    return READY


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


# Действие ноды, которым сверяется версия репозитория (node/service.py) —
# литералом, не импортом из bot/tools.py (тот тянет aiogram-независимый, но
# куда более тяжёлый набор зависимостей, а константа нужна тут одна).
_CHECK_UPDATE_ACTION = "check_update"


async def check_updates_summary(node_link: ServiceLink) -> str:
    """Сводка обновлений по всему рою — одна строка для человека.

    Версия репозитория одна на весь рой, и обновления у всех нод одни и те
    же (решение пользователя 2026-08-04) — вместо check_update по каждой
    ноде отдельно один вызов: последний тег плюс кто отстал.

    Своя нода спрашивается ОДИН раз через check_update (там уже есть
    latest_tag против репозитория) — остальные ноды не переспрашивают то же
    самое сетью, их версии берутся из get_state() (collect_reports, тот же
    веерный опрос, что и у /swarm и swarm_status).

    Общее ядро для тула node_manage/check_all (bot/tools.py) и кнопки
    «Проверить обновления» на панели /swarm (bot/handlers/swarm_panel.py) —
    раньше жило только в tools.py::_node_check_all.
    """
    own = await fetch_state(node_link, None)
    if own is None:
        return "недоступно: своя нода не отвечает"
    try:
        own_check = await node_link.command(_CHECK_UPDATE_ACTION, {}, dst=None)
    except ProtoError as exc:
        return f"не вышло проверить обновления: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: своя нода не ответила ({exc})"
    latest = own_check.get("latest")
    if not latest:
        return "не удалось узнать последнюю версию (сеть?)"

    reports = await collect_reports(node_link, own, with_monitor=False)
    behind: list[str] = []
    restart_only: list[str] = []
    unreachable: list[str] = []
    for r in reports:
        if not r.alive or r.state is None:
            unreachable.append(r.node_id)
            continue
        running = r.state.get("version")
        if running and running != latest:
            behind.append(r.node_id)
            continue
        # На последней версии, но, возможно, update уже лежит на диске, а
        # restart_node ещё не выполняли (node/service.py::get_state).
        if (r.state.get("update") or {}).get("restart_required"):
            restart_only.append(r.node_id)

    parts = [f"Последняя версия: v{latest}."]
    if behind:
        parts.append("Нужен update: " + ", ".join(sorted(behind)) + ".")
    if restart_only:
        parts.append(
            "Update уже на диске, нужен только restart_node: "
            + ", ".join(sorted(restart_only))
            + "."
        )
    if unreachable:
        parts.append("Не отвечают, не проверить: " + ", ".join(sorted(unreachable)) + ".")
    if not behind and not restart_only:
        parts.append("Все доступные ноды на последней версии.")
    return " ".join(parts)
