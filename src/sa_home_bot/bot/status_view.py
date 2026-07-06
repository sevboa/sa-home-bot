"""Панель /status: inline-клавиатура действий + общие построители текстов.

Билдеры вызываются и из message-хендлеров (ввод команды вручную), и из
callback-хендлеров (нажатие кнопки), поэтому логика собрана здесь в одном
месте. Данные о здоровье/дисках/статистике приходят от службы monitor по
протоколу (MonitorLink); недоступный монитор — честный текст об этом, а не
исключение в чат. Локальным остался только /downtime (журнал `last` этой же
машины).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, scan_limit
from sa_home_bot.bot.monitor_link import MonitorLink, MonitorUnavailableError
from sa_home_bot.bot.monitor_state import (
    parse_disk_summary,
    parse_health_state,
    parse_outage,
)
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import KIND_CPU
from sa_home_bot.domain.render import (
    render_downtime,
    render_stats,
    render_status_full,
    render_status_summary,
)
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.sensors.power import read_power_events_sync
from sa_home_bot.subscriptions.models import Subscription

# Подписи кнопок для callback-кодов (см. commands.STATUS_ACTIONS).
_LABELS: dict[str, str] = {
    "full": "🔎 Подробно",
    "stats": "📈 Статистика",
    "downtime": "⏻ Отключения",
    "scan": "🔄 Скан",
}

# Постранично по 10 отключений (см. sensors.power.read_power_events_sync).
DOWNTIME_PAGE_SIZE = 10

# id действия монитора (объявлено в его describe).
MONITOR_ACTION_SCAN = "scan_now"

MONITOR_DOWN_TEXT = (
    "⚠️ Служба мониторинга недоступна — бот не может получить данные датчиков. "
    "Проверьте, запущен ли монитор."
)


def build_status_keyboard(
    subscription: Subscription | None,
) -> InlineKeyboardMarkup | None:
    """Кнопки под /status — только те действия, что разрешены подписке."""
    buttons = [
        InlineKeyboardButton(text=_LABELS[code], callback_data=f"{commands.CALLBACK_PREFIX}:{code}")
        for code, cmd in commands.STATUS_ACTIONS.items()
        if subscription is not None and subscription.allows_command(cmd.name)
    ]
    if not buttons:
        return None
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_summary_text(link: MonitorLink) -> str:
    try:
        state = await link.get_state()
    except (MonitorUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    states = [parse_health_state(h) for h in state.get("health", [])]
    cpu_states = [s for s in states if s.kind == KIND_CPU]
    disks = [parse_disk_summary(d) for d in state.get("disks", [])]
    uptime_s = state.get("uptime_s")
    thresholds = state.get("thresholds", {})
    cpu_th = thresholds.get("cpu", {})
    disk_th = thresholds.get("disk", {})
    return render_status_summary(
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


async def build_full_text(link: MonitorLink) -> str:
    try:
        state = await link.get_state()
    except (MonitorUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    return render_status_full([parse_health_state(h) for h in state.get("health", [])])


async def build_stats_text(link: MonitorLink) -> str:
    try:
        state = await link.get_state()
    except (MonitorUnavailableError, ProtoError):
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


async def build_scan_text(store: Store, link: MonitorLink) -> str:
    """Ручной форс-скан через монитор с лимитом (раз в минуту, 5 в сутки).

    Слот лимита расходуется только когда монитор реально поставил новый job
    (если оба скана уже в его очереди — метку не пишем). Метки лимита — в БД
    бота: лимит защищает от спама кнопкой, это забота фронтенда.
    """
    now = datetime.now(tz=UTC)
    decision = scan_limit.decide(await store.get_manual_scan_ticks(), now)
    if not decision.allowed:
        return decision.reason
    try:
        result = await link.command(MONITOR_ACTION_SCAN)
    except (MonitorUnavailableError, ProtoError):
        return MONITOR_DOWN_TEXT
    if not (result.get("sensor_queued") or result.get("smart_queued")):
        return "⏳ Скан уже в очереди — дождитесь результата."
    await store.set_manual_scan_ticks(list(decision.ticks))
    return "🔄 Скан датчиков и дисков поставлен в очередь."
