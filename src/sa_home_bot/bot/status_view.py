"""Панель /status: inline-клавиатура действий + общие построители текстов.

Билдеры вызываются и из message-хендлеров (ввод команды вручную), и из
callback-хендлеров (нажатие кнопки), поэтому логика собрана здесь в одном месте.
Блокирующие чтения (`last`, `journalctl`, psutil) уходят в executor.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import KIND_CPU
from sa_home_bot.domain.render import (
    render_downtime,
    render_stats,
    render_status_full,
    render_status_summary,
)
from sa_home_bot.jobs.scan import SensorScanJob
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


async def build_summary_text(store: Store, disk_specs: list[str]) -> str:
    loop = asyncio.get_running_loop()
    states = await store.get_all_states()
    cpu_states = [s for s in states if s.kind == KIND_CPU]
    uptime = await loop.run_in_executor(None, read_uptime_sync)
    disks = await loop.run_in_executor(None, read_disk_summaries_sync, disk_specs)
    events = await loop.run_in_executor(None, read_power_events_sync, 1)
    last_outage = events[0] if events else None
    return render_status_summary(
        datetime.now(tz=UTC), uptime, cpu_states, disks, last_outage
    )


async def build_full_text(store: Store) -> str:
    return render_status_full(await store.get_all_states())


async def build_stats_text(store: Store) -> str:
    counts = await store.job_run_counts()
    runs = await store.recent_job_runs(limit=8)
    return render_stats(counts, runs)


async def build_downtime_text() -> str:
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, read_power_events_sync, 10)
    return render_downtime(events)


async def build_scan_text(queue: DedupQueue) -> str:
    queued = await queue.put(SensorScanJob())
    if queued:
        return "🔄 Скан поставлен в очередь."
    return "⏳ Скан уже в очереди — дождитесь результата."
