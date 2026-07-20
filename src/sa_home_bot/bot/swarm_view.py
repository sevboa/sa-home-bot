"""Сводка роя (/swarm, алиас /nodes): агрегатная шапка + строка на ноду.

Данные собираются веером через одну свою ноду («спроси любого»): для каждой
живой ноды — get_state её сервиса node и её монитора, всё параллельно
(ProtoClient мультиплексирует запросы по id конверта) с коротким таймаутом
на запрос — зависший пир не тормозит сводку, мёртвому запросов не шлём.

Шапка — плотные факты без воздуха: сколько нод/в сети, разъезд версий ПО
(ноды обновляются вручную — отставшие видны сразу), последний сбой по рою.
Ограничение честности: у каждой ноды виден только ПОСЛЕДНИЙ outage — если
после сбоя было штатное выключение, сбой из шапки исчезает (доп. запросов
к истории ради этого сознательно не делаем).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands, node_links, node_view, status_view, wake_state
from sa_home_bot.bot.monitor_state import parse_outage
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import WakeConfig
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import KIND_CPU, POWER_UNEXPECTED, PowerEvent
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.version import version_key

SWARM_HEADER = "🕸 <b>Рой</b>"

# Домашний ПК известен рою только адресом для WoL, своей ноды на нём ещё нет.
REMOTE_STUB_TEXT = (
    "💻 <b>Домашний ПК</b> — вне роя (нода ещё не развёрнута), Wake-on-LAN"
)

WAKE_BUTTON_TEXT = "🔌 Разбудить ПК"

# Таймаут одного запроса к пиру: зависший (но формально подключённый) пир
# не должен держать сводку дольше этого.
PEER_TIMEOUT_S = 3.0


@dataclass
class _NodeReport:
    node_id: str
    alive: bool
    state: dict | None = None  # get_state сервиса node (None — не ответил)
    monitor: dict | None = None  # get_state монитора (None — не ответил/нет)


async def _fetch(node_link: ServiceLink, dst: Address | None) -> dict | None:
    try:
        return await asyncio.wait_for(node_link.get_state(dst=dst), PEER_TIMEOUT_S)
    except (ServiceUnavailableError, ProtoError, TimeoutError):
        return None


async def _collect(node_link: ServiceLink, own_state: dict) -> list[_NodeReport]:
    """Параллельный сбор состояний всех нод роя (своя — первой)."""
    own = _NodeReport(node_id=own_state.get("node", "?"), alive=True, state=own_state)
    reports = [own]
    for peer in own_state.get("peers", []):
        pid = peer.get("id", "?")
        reports.append(_NodeReport(node_id=pid, alive=bool(peer.get("alive"))))

    async def fill(report: _NodeReport) -> None:
        node_dst = (
            Address(node=report.node_id, service=node_view.NODE_SERVICE)
            if report.state is None
            else None
        )
        if report.state is None:
            report.state = await _fetch(node_link, node_dst)
        report.monitor = await _fetch(
            node_link, Address(node=report.node_id, service=status_view.MONITOR_SERVICE)
        )

    await asyncio.gather(*(fill(r) for r in reports if r.alive))
    return reports


def _versions_line(reports: list[_NodeReport]) -> str | None:
    versions = {
        r.node_id: r.state["version"]
        for r in reports
        if r.state is not None and r.state.get("version")
    }
    if not versions:
        return None
    latest = max(versions.values(), key=version_key)
    stale = {nid: v for nid, v in versions.items() if v != latest}
    if not stale:
        return f"ПО: v{latest} у всех"
    lagging = ", ".join(f"{nid} (v{v})" for nid, v in sorted(stale.items()))
    return f"ПО: свежая v{latest} · отстаёт {lagging}"


def _last_failure_line(reports: list[_NodeReport], now: datetime) -> str | None:
    """Самый свежий внезапный сбой по рою (см. ограничение в докстроке модуля)."""
    freshest: tuple[datetime, str, PowerEvent] | None = None
    for r in reports:
        if r.monitor is None:
            continue
        outage = parse_outage(r.monitor.get("last_outage"))
        if outage is None or outage.kind != POWER_UNEXPECTED:
            continue
        moment = outage.down_at or outage.boot_at
        if freshest is None or moment > freshest[0]:
            freshest = (moment, r.node_id, outage)
    if freshest is None:
        return None
    moment, node_id, _ = freshest
    ago = format_duration((now - moment).total_seconds())
    return f"Последний сбой: {node_id}, {ago} назад"


def _cpu_max(monitor: dict) -> float | None:
    temps = [
        h.get("temperature_c")
        for h in monitor.get("health", [])
        if h.get("kind") == KIND_CPU and h.get("temperature_c") is not None
    ]
    return max(temps) if temps else None


def _node_line(report: _NodeReport) -> str:
    name = node_links.node_command(report.node_id) or f"<b>{report.node_id}</b>"
    if not report.alive:
        return f"{node_view.LAMP_RED} {name} — не в сети"
    if report.state is None:
        return f"{node_view.LAMP_RED} {name} — не отвечает"

    bits = [f"v{report.state.get('version', '?')}"]
    services = report.state.get("services", [])
    running = sum(1 for s in services if s.get("status") == "running")
    bits.append(f"службы {running}/{len(services)}")

    if report.monitor is None:
        bits.append("монитор не отвечает")
    else:
        cpu = _cpu_max(report.monitor)
        if cpu is not None:
            bits.append(f"CPU {cpu:.0f}°C")
        alerting = sum(
            1 for h in report.monitor.get("health", []) if h.get("status") == "alerting"
        )
        if alerting:
            bits.append(f"🔔 {alerting}")
        if report.monitor.get("requirements"):
            bits.append("⚠️")
    return f"{node_view.LAMP_GREEN} {name} · " + " · ".join(bits)


def render_swarm(
    reports: list[_NodeReport], wake: WakeConfig | None, now: datetime
) -> str:
    total = len(reports)
    online = sum(1 for r in reports if r.alive and r.state is not None)
    lines = [f"{SWARM_HEADER}: {total} нод, в сети {online}"]
    for extra in (_versions_line(reports), _last_failure_line(reports, now)):
        if extra:
            lines.append(extra)
    lines.append("")
    lines.extend(_node_line(r) for r in reports)
    if wake is not None and wake.mac:
        lines.append(REMOTE_STUB_TEXT)
    return "\n".join(lines)


def _wake_rows(
    subscription: Subscription, wake: WakeConfig | None
) -> list[InlineKeyboardButton]:
    """Ручная кнопка — фиксированная машина из [wake] (запасной путь)."""
    if wake is None or not wake.mac or not subscription.allows_command(commands.WAKE.name):
        return []
    return [InlineKeyboardButton(text=WAKE_BUTTON_TEXT, callback_data=commands.wake_callback())]


async def _offline_wake_rows(
    subscription: Subscription, store: Store, reports: Sequence[_NodeReport]
) -> list[InlineKeyboardButton]:
    """Точечная кнопка на каждую уснувшую ноду, чьи реквизиты уже известны
    (см. wake_state.remember, вызывается ниже при сборе сводки).

    «Недоступна для будильника» — не только формально disconnected
    (``alive=False``, «не в сети» в _node_line), но и «не отвечает»
    (``alive=True``, но get_state не дозвался, ``state is None``) — то же
    промежуточное состояние, в котором PeerLink ещё не обнаружил обрыв
    (TCP keepalive обнаруживает пропажу пира не мгновенно, см.
    proto/client.py). Иначе пользователь видит "не отвечает" и не может
    разбудить именно тогда, когда это и нужно (живая находка 2026-07-20).
    """
    if not subscription.allows_command(commands.WAKE.name):
        return []
    buttons = []
    for r in reports:
        if r.alive and r.state is not None:
            continue
        if await wake_state.cached(store, r.node_id) is None:
            continue
        buttons.append(
            InlineKeyboardButton(
                text=f"🔌 Разбудить {r.node_id}",
                callback_data=commands.wake_callback(r.node_id),
            )
        )
    return buttons


async def _remember_wake_info(store: Store, reports: Sequence[_NodeReport]) -> None:
    for r in reports:
        if r.state is not None:
            await wake_state.remember(store, r.node_id, r.state.get("wake"))


async def build_swarm_keyboard(
    subscription: Subscription | None,
    wake: WakeConfig | None,
    reports: Sequence[_NodeReport] = (),
    store: Store | None = None,
) -> InlineKeyboardMarkup | None:
    """Только действия (wake) — навигация к нодам идёт ссылками в тексте."""
    if subscription is None:
        return None
    buttons = _wake_rows(subscription, wake)
    if store is not None:
        buttons = buttons + await _offline_wake_rows(subscription, store, reports)
    return actions.rows(buttons)


async def find_lan_waker(
    node_link: ServiceLink, store: Store, target_node_id: str, target_broadcast: str
) -> str | None:
    """Живая нода в том же сегменте LAN, что и уснувшая ``target_node_id``
    (совпадает объявленный ею broadcast) — она отправит magic packet вместо
    бота, который вполне может крутиться вне этой локалки (этап 19 п.6).

    Заодно свежит кэш wake-реквизитов всех увиденных сейчас нод — тот же
    fan-out, что и сводка /swarm, отдельного запроса не требует.
    """
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return None
    reports = await _collect(node_link, own_state)
    await _remember_wake_info(store, reports)
    for r in reports:
        if not r.alive or r.node_id == target_node_id or r.state is None:
            continue
        candidate = r.state.get("wake")
        if candidate and candidate.get("broadcast") == target_broadcast:
            return r.node_id
    return None


async def build_swarm_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    wake: WakeConfig | None = None,
    store: Store | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    try:
        own_state = await node_link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return node_view.NODE_DOWN_TEXT, None
    reports = await _collect(node_link, own_state)
    if store is not None:
        await _remember_wake_info(store, reports)
    text = render_swarm(reports, wake, datetime.now(tz=UTC))
    keyboard = await build_swarm_keyboard(subscription, wake, reports, store)
    return text, keyboard
