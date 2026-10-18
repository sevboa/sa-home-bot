from __future__ import annotations

import logging
from typing import Any

from sa_home_bot.bot import tool_debug
from sa_home_bot.bot.notifier import Notifier, broadcast
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.domain.models import EVENT_SYSTEM, POWER_UNEXPECTED, PowerEvent
from sa_home_bot.domain.render import render_outage_line
from sa_home_bot.runtime import format_duration
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)


def render_startup(clean: bool, last_outage: PowerEvent | None = None) -> str:
    if clean:
        return "🟢 <b>Сторож снова на посту.</b>\nЗапуск после штатного завершения."
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


async def broadcast_system(book: SubscriptionBook, notifier: Notifier, text: str) -> int:
    sent = await broadcast(book.accepting(EVENT_SYSTEM), notifier, text)
    log.info("Системное событие разослано %d подписчикам", sent)
    return sent


async def broadcast_all(book: SubscriptionBook, notifier: Notifier, text: str) -> int:
    alive = (sub for sub in book.all() if not sub.broken)
    sent = await broadcast(alive, notifier, text)
    log.info("Событие разослано %d подписчикам (broadcast_all)", sent)
    return sent


EVENT_ALFRED_TOOL_CALL = "alfred_tool_call"


async def notify_tool_call(
    book: SubscriptionBook,
    notifier: Notifier,
    tool_name: str,
    *,
    args: dict[str, Any] | None = None,
    result: str | None = None,
    debug: ToolCalls | None = None,
) -> None:
    text = tool_debug.render_short(tool_name)
    markup = None
    if debug is not None and args is not None and result is not None:
        token = debug.add(tool_debug.ToolCall(name=tool_name, args=args, result=result))
        markup = tool_debug.keyboard(token, expanded=False)
    opted_in = (
        sub for sub in book.all() if not sub.broken and EVENT_ALFRED_TOOL_CALL in sub.event_types
    )
    await broadcast(opted_in, notifier, text, reply_markup=markup)
