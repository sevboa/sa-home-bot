"""Фоновый опрос очереди напоминаний (тул remind, /ai, LLM_INTEGRATION_PLAN.md §8.5).

Живёт в процессе бота, не monitor — только бот держит Telegram Bot/Notifier.
Простой поллинг, не отдельный APScheduler: интервал не критичен (минуты, не
секунды), заводить вторую библиотеку-планировщик ради одной очереди не нужно
(monitor уже использует APScheduler для своих cron-задач — reminders) —
принципиально другой паттерн (разовое время, не cron), не тот же код.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime

from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.db.store import Store

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 30.0
# Фиксированная строка персонажа (как ARNOLD_WAKING/CLOSING_TEXT в
# ai_flow.py) — доставка не идёт через LLM, это не её ход.
REMINDER_PREFIX = "<b>Альфред:</b> Просили напомнить: "


async def reminder_loop(store: Store, notifier: Notifier) -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        try:
            await _fire_due(store, notifier)
        except Exception:  # noqa: BLE001 — сбой одного тика не должен ронять цикл
            log.exception("reminders: сбой опроса очереди")


async def _fire_due(store: Store, notifier: Notifier) -> None:
    now = datetime.now(tz=UTC)
    for row in await store.due_reminders(now):
        text = REMINDER_PREFIX + html.escape(row["text"])
        await notifier.send_direct(row["chat_id"], text)
        await store.mark_reminder_fired(row["id"], now)
