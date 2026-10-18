from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot import actions, commands
from sa_home_bot.bot.monitor_state import (
    parse_disk_summary,
    parse_health_state,
    parse_host_metric_state,
    parse_outage,
)
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.ui.callback_data import pack
from sa_home_bot.bot.ui.keyboards import button
from sa_home_bot.domain.models import KIND_CPU, KIND_GPU
from sa_home_bot.domain.render import (
    render_downtime,
    render_host_block,
    render_stats,
    render_status_full,
    render_status_summary,
)
from sa_home_bot.proto.addresses import MONITOR_SERVICE
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS
from sa_home_bot.proto.messages import (
    ERR_UNKNOWN_ACTION,
    ERR_UNKNOWN_DST,
    ActionSpec,
    Address,
    ProtoError,
)
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.clock import utc_now

_LABELS: dict[str, str] = {
    "full": "🔎 Подробно",
    "stats": "📈 Статистика",
    "downtime": "⏻ Отключения",
}

DOWNTIME_PAGE_SIZE = 10

MONITOR_DOWN_TEXT = (
    "⚠️ Служба мониторинга недоступна — бот не может получить данные датчиков. "
    "Проверьте, запущен ли монитор."
)

DOWNTIME_UNSUPPORTED_TEXT = (
    "⚠️ Монитор этой ноды не поддерживает историю отключений — обновите ноду."
)


def monitor_dst(node_id: str | None) -> Address:
    return Address(node=node_id, service=MONITOR_SERVICE)


def _node_tail(node_id: str | None) -> tuple[str, ...]:
    return (node_id,) if node_id else ()


def build_status_keyboard(
    subscription: Subscription | None,
    monitor_actions: Sequence[ActionSpec] = (),
    node_id: str | None = None,
) -> InlineKeyboardMarkup | None:
    if subscription is None:
        return None
    buttons = [
        button(_LABELS[code], pack(commands.CALLBACK_PREFIX, code, *_node_tail(node_id)))
        for code, cmd in commands.STATUS_ACTIONS.items()
        if subscription.allows_command(cmd.name)
    ]
    buttons.extend(
        button(
            action.title,
            commands.action_callback(action.id, node_id=node_id, service=MONITOR_SERVICE),
        )
        for action in monitor_actions
        if not action.params and subscription.allows_action(action.id, MONITOR_SERVICE)
    )
    return actions.rows(buttons)


async def _monitor_state(link: ServiceLink, dst: Address | None) -> dict | None:
    try:
        return await link.get_state(dst=dst)
    except SERVICE_CALL_ERRORS:
        return None


async def build_summary_text(link: ServiceLink, dst: Address | None = None) -> str:
    state = await _monitor_state(link, dst)
    if state is None:
        return MONITOR_DOWN_TEXT
    states = [parse_health_state(h) for h in state.get("health", [])]
    cpu_states = [s for s in states if s.kind == KIND_CPU]
    gpu_states = [s for s in states if s.kind == KIND_GPU]
    disks = [parse_disk_summary(d) for d in state.get("disks", [])]
    uptime_s = state.get("uptime_s")
    thresholds = state.get("thresholds", {})
    cpu_th = thresholds.get("cpu", {})
    gpu_th = thresholds.get("gpu", {})
    disk_th = thresholds.get("disk", {})
    text = render_status_summary(
        utc_now(),
        timedelta(seconds=uptime_s) if uptime_s is not None else None,
        cpu_states,
        gpu_states,
        disks,
        parse_outage(state.get("last_outage")),
        cpu_warn_c=cpu_th.get("warn_c", 0.0),
        cpu_crit_c=cpu_th.get("crit_c", 0.0),
        gpu_warn_c=gpu_th.get("warn_c", 0.0),
        gpu_crit_c=gpu_th.get("crit_c", 0.0),
        disk_warn_c=disk_th.get("warn_c", 0.0),
        disk_crit_c=disk_th.get("crit_c", 0.0),
    )
    host_states = [parse_host_metric_state(h) for h in state.get("host_health", [])]
    host_block = render_host_block(host_states)
    if host_block:
        text += "\n\n" + host_block
    problems = state.get("requirements") or []
    if problems:
        text += "\n" + "\n".join(f"⚠️ {p['hint']}" for p in problems)
    return text


async def build_full_text(link: ServiceLink, dst: Address | None = None) -> str:
    state = await _monitor_state(link, dst)
    if state is None:
        return MONITOR_DOWN_TEXT
    return render_status_full([parse_health_state(h) for h in state.get("health", [])])


async def build_stats_text(link: ServiceLink, dst: Address | None = None) -> str:
    state = await _monitor_state(link, dst)
    if state is None:
        return MONITOR_DOWN_TEXT
    return render_stats(state.get("job_counts", {}), state.get("recent_runs", []))


def _downtime_callback(offset: int, node_id: str | None = None) -> str:
    return pack(commands.CALLBACK_PREFIX, commands.DOWNTIME_PAGE_CODE, offset, *_node_tail(node_id))


def _downtime_keyboard(
    offset: int, has_next: bool, node_id: str | None = None
) -> InlineKeyboardMarkup | None:
    buttons = []
    if offset > 0:
        prev_offset = max(0, offset - DOWNTIME_PAGE_SIZE)
        buttons.append(button("⬅️ Предыдущие 10", _downtime_callback(prev_offset, node_id)))
    if has_next:
        next_offset = offset + DOWNTIME_PAGE_SIZE
        buttons.append(button("➡️ Следующие 10", _downtime_callback(next_offset, node_id)))
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def build_downtime_page(
    link: ServiceLink, offset: int = 0, node_id: str | None = None
) -> tuple[str, InlineKeyboardMarkup | None]:
    try:
        result = await link.command(
            "downtime",
            {"offset": offset, "limit": DOWNTIME_PAGE_SIZE},
            dst=monitor_dst(node_id),
        )
    except ServiceUnavailableError:
        return MONITOR_DOWN_TEXT, None
    except ProtoError as exc:
        if exc.code in (ERR_UNKNOWN_ACTION, ERR_UNKNOWN_DST):
            return DOWNTIME_UNSUPPORTED_TEXT, None
        return MONITOR_DOWN_TEXT, None
    events = [e for e in (parse_outage(raw) for raw in result.get("events", [])) if e is not None]
    text = render_downtime(events, offset)
    keyboard = _downtime_keyboard(offset, bool(result.get("has_next")), node_id)
    return text, keyboard
