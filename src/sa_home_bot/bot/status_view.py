"""Панель /status: inline-клавиатура действий + общие построители текстов.

Билдеры вызываются и из message-хендлеров (ввод команды вручную), и из
callback-хендлеров (нажатие кнопки), поэтому логика собрана здесь в одном
месте. Данные о здоровье/дисках/статистике приходят от службы monitor по
протоколу (ServiceLink); недоступный монитор — честный текст об этом, а не
исключение в чат. Локальным остался только /downtime (журнал `last` этой же
машины).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.monitor_state import (
    parse_disk_summary,
    parse_health_state,
    parse_outage,
)
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.domain.models import KIND_CPU
from sa_home_bot.domain.render import (
    render_downtime,
    render_stats,
    render_status_full,
    render_status_summary,
)
from sa_home_bot.proto.messages import ActionSpec, Address, ProtoError
from sa_home_bot.sensors.power import read_power_events_sync
from sa_home_bot.subscriptions.models import Subscription

# Подписи кнопок-представлений (см. commands.STATUS_ACTIONS).
_LABELS: dict[str, str] = {
    "full": "🔎 Подробно",
    "stats": "📈 Статистика",
    "downtime": "⏻ Отключения",
}

# Постранично по 10 отключений (см. sensors.power.read_power_events_sync).
DOWNTIME_PAGE_SIZE = 10

MONITOR_DOWN_TEXT = (
    "⚠️ Служба мониторинга недоступна — бот не может получить данные датчиков. "
    "Проверьте, запущен ли монитор."
)

MONITOR_SERVICE = "monitor"


def build_status_keyboard(
    subscription: Subscription | None,
    monitor_actions: Sequence[ActionSpec] = (),
) -> InlineKeyboardMarkup | None:
    """Кнопки под /status: представления бота + действия монитора из describe.

    Представления (подробно/статистика/отключения) — свои у бота, права по
    имени команды. Действия — динамические из describe монитора, права
    `действие@monitor` (или голое имя действия — совместимость).
    """
    if subscription is None:
        return None
    buttons = [
        InlineKeyboardButton(text=_LABELS[code], callback_data=f"{commands.CALLBACK_PREFIX}:{code}")
        for code, cmd in commands.STATUS_ACTIONS.items()
        if subscription.allows_command(cmd.name)
    ]
    for action in monitor_actions:
        if action.params:  # действия с параметрами в /status не выносим
            continue
        if subscription.allows_action(action.id, MONITOR_SERVICE):
            buttons.append(
                InlineKeyboardButton(
                    text=action.title,
                    callback_data=(
                        f"{commands.ACTION_CALLBACK_PREFIX}:{MONITOR_SERVICE}:{action.id}"
                    ),
                )
            )
    if not buttons:
        return None
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_summary_text(link: ServiceLink, dst: Address | None = None) -> str:
    try:
        state = await link.get_state(dst=dst)
    except (ServiceUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    states = [parse_health_state(h) for h in state.get("health", [])]
    cpu_states = [s for s in states if s.kind == KIND_CPU]
    disks = [parse_disk_summary(d) for d in state.get("disks", [])]
    uptime_s = state.get("uptime_s")
    thresholds = state.get("thresholds", {})
    cpu_th = thresholds.get("cpu", {})
    disk_th = thresholds.get("disk", {})
    text = render_status_summary(
        datetime.now(tz=UTC),
        timedelta(seconds=uptime_s) if uptime_s is not None else None,
        cpu_states,
        disks,
        parse_outage(state.get("last_outage")),
        cpu_warn_c=cpu_th.get("warn_c", 0.0),
        cpu_crit_c=cpu_th.get("crit_c", 0.0),
        disk_warn_c=disk_th.get("warn_c", 0.0),
        disk_crit_c=disk_th.get("crit_c", 0.0),
    )
    problems = state.get("requirements") or []
    if problems:
        text += "\n" + "\n".join(f"⚠️ {p['hint']}" for p in problems)
    return text


async def build_full_text(link: ServiceLink) -> str:
    try:
        state = await link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    return render_status_full([parse_health_state(h) for h in state.get("health", [])])


async def build_stats_text(link: ServiceLink) -> str:
    try:
        state = await link.get_state()
    except (ServiceUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    return render_stats(state.get("job_counts", {}), state.get("recent_runs", []))


def _downtime_callback(offset: int) -> str:
    return f"{commands.CALLBACK_PREFIX}:{commands.DOWNTIME_PAGE_CODE}:{offset}"


def _downtime_keyboard(offset: int, has_next: bool) -> InlineKeyboardMarkup | None:
    """Кнопки «Предыдущие 10» / «Следующие 10» под страницей /downtime."""
    buttons = []
    if offset > 0:
        prev_offset = max(0, offset - DOWNTIME_PAGE_SIZE)
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Предыдущие 10", callback_data=_downtime_callback(prev_offset)
            )
        )
    if has_next:
        next_offset = offset + DOWNTIME_PAGE_SIZE
        buttons.append(
            InlineKeyboardButton(
                text="➡️ Следующие 10", callback_data=_downtime_callback(next_offset)
            )
        )
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


async def build_downtime_page(offset: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст + клавиатура одной страницы /downtime (по 10 отключений)."""
    loop = asyncio.get_running_loop()
    events, has_next = await loop.run_in_executor(
        None, read_power_events_sync, offset, DOWNTIME_PAGE_SIZE
    )
    text = render_downtime(events, offset)
    keyboard = _downtime_keyboard(offset, has_next)
    return text, keyboard


