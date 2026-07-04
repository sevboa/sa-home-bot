"""Системные события жизненного цикла: старт (clean/crash), shutdown, восстановление связи.

Тип события — `system`; получают подписки с `system` или `*` в event_types.
"""

from __future__ import annotations

import logging

from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.domain.models import EVENT_SYSTEM, POWER_UNEXPECTED, PowerEvent
from sa_home_bot.domain.render import render_outage_line
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)


def render_startup(clean: bool, last_outage: PowerEvent | None = None) -> str:
    if clean:
        return "🟢 <b>Сторож снова на посту.</b>\nЗапуск после штатного завершения."
    # Нештатное завершение: если последнее отключение машины — потеря питания
    # (внезапный обрыв), прикладываем его детали (когда, простой).
    if last_outage is not None and last_outage.kind == POWER_UNEXPECTED:
        return (
            "🟠 <b>Сторож восстановился после сбоя.</b>\n"
            "Похоже, была потеря питания или зависание машины.\n\n"
            "Последнее отключение:\n" + render_outage_line(last_outage)
        )
    return (
        "🟠 <b>Сторож восстановился после сбоя.</b>\n"
        "Предыдущая сессия завершилась нештатно (краш или потеря питания)."
    )


def render_shutdown() -> str:
    return (
        "⚫️ <b>Сторож уходит в офлайн.</b>\n"
        "Штатное завершение — мониторинг приостановлен."
    )


def render_link_restored(downtime_seconds: float) -> str:
    return (
        "🔌 <b>Связь с Telegram восстановлена.</b>\n"
        f"Были офлайн ~{format_duration(downtime_seconds)}."
    )


async def broadcast_system(
    book: SubscriptionBook, notifier: Notifier, text: str
) -> int:
    sent = 0
    for sub in book.accepting(EVENT_SYSTEM):
        if await notifier.send_direct(sub.chat_id, text) is not None:
            sent += 1
    log.info("Системное событие разослано %d подписчикам", sent)
    return sent
