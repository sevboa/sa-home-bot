"""Панель /status: inline-клавиатура действий + общие построители текстов.

Билдеры вызываются и из message-хендлеров (ввод команды вручную), и из
callback-хендлеров (нажатие кнопки), поэтому логика собрана здесь в одном месте.
Блокирующие чтения (`last`, `journalctl`, psutil) уходят в executor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, scan_limit
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import KIND_CPU
from sa_home_bot.domain.render import (
    render_downtime,
    render_stats,
    render_status_full,
    render_status_summary,
)
from sa_home_bot.jobs.scan import SensorScanJob
from sa_home_bot.jobs.smart import SmartScanJob
from sa_home_bot.sensors.disks import read_disk_summaries_sync
from sa_home_bot.sensors.power import read_power_events_sync, read_uptime_sync
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.worker.queue import DedupQueue

# Подписи кнопок для callback-кодов (см. commands.STATUS_ACTIONS).
_LABELS: dict[str, str] = {
    "full": "🔎 Подробно",
    "stats": "📈 Статистика",
    "downtime": "⏻ Отключения",
    "scan": "🔄 Скан",
}

# Постранично по 10 отключений (см. sensors.power.read_power_events_sync).
DOWNTIME_PAGE_SIZE = 10


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


async def build_summary_text(store: Store, config: Settings) -> str:
    loop = asyncio.get_running_loop()
    states = await store.get_all_states()
    cpu_states = [s for s in states if s.kind == KIND_CPU]
    # Иконка здоровья диска — из снимков БД (совпадает с алертами SmartScanJob);
    # температура/место — на лету.
    health_map = await store.get_smart_health_map()
    uptime = await loop.run_in_executor(None, read_uptime_sync)
    disks = await loop.run_in_executor(
        None, read_disk_summaries_sync, list(config.sensors.disks.devices), health_map
    )
    events, _ = await loop.run_in_executor(None, read_power_events_sync, 0, 1)
    last_outage = events[0] if events else None
    return render_status_summary(
        datetime.now(tz=UTC),
        uptime,
        cpu_states,
        disks,
        last_outage,
        cpu_warn_c=config.sensors.cpu.warn_c,
        cpu_crit_c=config.sensors.cpu.crit_c,
        disk_warn_c=config.sensors.disks.warn_c,
        disk_crit_c=config.sensors.disks.crit_c,
    )


async def build_full_text(store: Store) -> str:
    return render_status_full(await store.get_all_states())


async def build_stats_text(store: Store) -> str:
    counts = await store.job_run_counts()
    runs = await store.recent_job_runs(limit=8)
    return render_stats(counts, runs)


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


async def build_scan_text(store: Store, queue: DedupQueue) -> str:
    """Ручной форс-скан датчиков + дисков с лимитом (раз в минуту, 5 в сутки).

    Слот лимита расходуется только когда реально поставлен новый job (если оба
    скана уже в очереди — метку не пишем).
    """
    now = datetime.now(tz=UTC)
    decision = scan_limit.decide(await store.get_manual_scan_ticks(), now)
    if not decision.allowed:
        return decision.reason
    sensor_queued = await queue.put(SensorScanJob())
    smart_queued = await queue.put(SmartScanJob())
    if not (sensor_queued or smart_queued):
        return "⏳ Скан уже в очереди — дождитесь результата."
    await store.set_manual_scan_ticks(list(decision.ticks))
    return "🔄 Скан датчиков и дисков поставлен в очередь."
