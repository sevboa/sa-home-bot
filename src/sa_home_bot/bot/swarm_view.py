from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot import wake_core
from sa_home_bot.bot import actions, commands, node_links, node_view, wake_state
from sa_home_bot.bot.monitor_state import parse_outage
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.keyboards import button
from sa_home_bot.config import WakeConfig
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import KIND_CPU, KIND_GPU, POWER_UNEXPECTED, PowerEvent
from sa_home_bot.node.kind import traits_for
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.version import version_key

SWARM_HEADER = "🕸 <b>Рой</b>"

REMOTE_STUB_TEXT = "💻 <b>Домашний ПК</b> — вне роя (нода ещё не развёрнута), Wake-on-LAN"

WAKE_BUTTON_TEXT = "🔌 Разбудить ПК"


def wake_node_text(node_id: str) -> str:
    return f"🔌 Разбудить {node_id}"


NodeReport = wake_core.NodeReport


def node_versions(reports: Sequence[NodeReport]) -> dict[str, str]:
    return {
        r.node_id: r.state["version"]
        for r in reports
        if r.state is not None and r.state.get("version")
    }


def latest_version(versions: dict[str, str]) -> str:
    return max(versions.values(), key=version_key)


def _versions_line(reports: Sequence[NodeReport]) -> str | None:
    versions = node_versions(reports)
    if not versions:
        return None
    latest = latest_version(versions)
    stale = {nid: v for nid, v in versions.items() if v != latest}
    if not stale:
        return f"ПО: v{latest} у всех"
    lagging = ", ".join(f"{nid} (v{v})" for nid, v in sorted(stale.items()))
    return f"ПО: свежая v{latest} · отстаёт {lagging}"


def last_failure_line(reports: list[NodeReport], now: datetime) -> str | None:
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


def _temp_max(monitor: dict, kind: str) -> float | None:
    temps = [
        h.get("temperature_c")
        for h in monitor.get("health", [])
        if h.get("kind") == kind and h.get("temperature_c") is not None
    ]
    return max(temps) if temps else None


def _health_bucket(report: NodeReport) -> int:
    if not report.alive:
        if report.left:
            return 2
        return 0 if traits_for(report.kind).always_on else 2
    if report.state is None:
        return 0
    services = report.state.get("services", [])
    if any(s.get("status") != "running" for s in services):
        return 0
    monitor = report.monitor
    if monitor is None:
        return 0
    if any(h.get("status") == "alerting" for h in monitor.get("health", [])):
        return 0
    if monitor.get("requirements"):
        return 0
    return 1


def order_by_health(reports: Sequence[NodeReport]) -> list[NodeReport]:
    return sorted(reports, key=_health_bucket)


def node_line(report: NodeReport) -> str:
    name = node_links.node_command(report.node_id) or f"<b>{report.node_id}</b>"
    if not report.alive:
        if report.left:
            return f"{node_view.LAMP_GRAY} {name} — выключена штатно"
        if not traits_for(report.kind).always_on:
            return f"{node_view.LAMP_GRAY} {name} — спит (это норма)"
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
        cpu = _temp_max(report.monitor, KIND_CPU)
        if cpu is not None:
            bits.append(f"CPU {cpu:.0f}°C")
        gpu = _temp_max(report.monitor, KIND_GPU)
        if gpu is not None:
            bits.append(f"GPU {gpu:.0f}°C")
        alerting = sum(1 for h in report.monitor.get("health", []) if h.get("status") == "alerting")
        if alerting:
            bits.append(f"🔔 {alerting}")
        if report.monitor.get("requirements"):
            bits.append("⚠️")
    return f"{node_view.LAMP_GREEN} {name} · " + " · ".join(bits)


def render_swarm(reports: list[NodeReport], wake: WakeConfig | None, now: datetime) -> str:
    total = len(reports)
    online = sum(1 for r in reports if r.alive and r.state is not None)
    lines = [f"{SWARM_HEADER}: {total} нод, в сети {online}"]
    for extra in (_versions_line(reports), last_failure_line(reports, now)):
        if extra:
            lines.append(extra)
    lines.append("")
    lines.extend(node_line(r) for r in order_by_health(reports))
    if wake is not None and wake.mac:
        lines.append(REMOTE_STUB_TEXT)
    return "\n".join(lines)


def wake_rows(subscription: Subscription, wake: WakeConfig | None) -> list[InlineKeyboardButton]:
    if wake is None or not wake.mac or not subscription.allows_command(commands.WAKE.name):
        return []
    return [button(WAKE_BUTTON_TEXT, commands.wake_callback())]


async def offline_wakeable_ids(
    subscription: Subscription, store: Store, reports: Sequence[NodeReport]
) -> list[str]:
    if not subscription.allows_command(commands.WAKE.name):
        return []
    ids = []
    for r in reports:
        if r.alive and r.state is not None:
            continue
        if r.kind and not traits_for(r.kind).wakeable:
            continue
        if await wake_state.cached(store, r.node_id) is None:
            continue
        ids.append(r.node_id)
    return ids


async def offline_wake_rows(
    subscription: Subscription, store: Store, reports: Sequence[NodeReport]
) -> list[InlineKeyboardButton]:
    ids = await offline_wakeable_ids(subscription, store, reports)
    return [button(wake_node_text(node_id), commands.wake_callback(node_id)) for node_id in ids]


async def remember_wake_info(store: Store, reports: Sequence[NodeReport]) -> None:
    for r in reports:
        if r.state is not None:
            await wake_state.remember(store, r.node_id, r.state.get("wake"))


async def build_swarm_keyboard(
    subscription: Subscription | None,
    wake: WakeConfig | None,
    reports: Sequence[NodeReport] = (),
    store: Store | None = None,
) -> InlineKeyboardMarkup | None:
    if subscription is None:
        return None
    buttons = wake_rows(subscription, wake)
    if store is not None:
        buttons = buttons + await offline_wake_rows(subscription, store, reports)
    return actions.rows(buttons)


async def build_swarm_view(
    node_link: ServiceLink,
    subscription: Subscription | None,
    wake: WakeConfig | None = None,
    store: Store | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    try:
        own_state = await node_link.get_state()
    except SERVICE_CALL_ERRORS:
        return node_view.NODE_DOWN_TEXT, None
    reports = await wake_core.collect_reports(node_link, own_state, with_monitor=True)
    if store is not None:
        await remember_wake_info(store, reports)
    text = render_swarm(reports, wake, utc_now())
    keyboard = await build_swarm_keyboard(subscription, wake, reports, store)
    return text, keyboard
