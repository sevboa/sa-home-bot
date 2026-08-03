"""Системные события жизненного цикла: старт (clean/crash), shutdown, восстановление связи.

Тип события — `system`; получают подписки с `system` или `*` в event_types.
"""

from __future__ import annotations

import logging
from typing import Any

from sa_home_bot.bot import tool_debug
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.tool_debug import ToolCalls
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


async def broadcast_all(book: SubscriptionBook, notifier: Notifier, text: str) -> int:
    """Буквально всем не-broken подпискам, без opt-in по event_types —
    для событий, которые обязаны дойти и до гостей (у тех event_types по
    умолчанию пуст, book.accepting() их бы не нашёл). Тот же приём, что
    уже применён в notify_tool_call ниже."""
    sent = 0
    for sub in book.all():
        if not sub.broken and await notifier.send_direct(sub.chat_id, text) is not None:
            sent += 1
    log.info("Событие разослано %d подписчикам (broadcast_all)", sent)
    return sent


# Дебаг-канал: факт вызова инструмента Alfred'ом (живой /ai и self-scheduled
# remind через службу tasks). Само сообщение короткое — «обращался ли и в
# какой тул» (решение пользователя 2026-07-27); вход и выход прячутся за
# кнопкой «развернуть» (bot/tool_debug.py, просьба пользователя 2026-07-29):
# на каждой реплике вываливать килобайты выдачи в чат незачем, но когда
# модель ищет и не находит — без них не разобраться.
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
    """``debug``/``args``/``result`` — если известны, к сообщению добавляется
    кнопка «развернуть». У события от службы tasks (bot/node_events.py) их
    нет: та шлёт по рою только имя тула, и кнопке нечего показывать."""
    text = tool_debug.render_short(tool_name)
    markup = None
    if debug is not None and args is not None and result is not None:
        token = debug.add(tool_debug.ToolCall(name=tool_name, args=args, result=result))
        markup = tool_debug.keyboard(token, expanded=False)
    # Живая находка 2026-07-27: НЕ book.accepting() — тот трактует "*" в
    # event_types как "всё", а у админской подписки event_types=["*"]
    # (полный охват системных событий). На вызов тула, происходящий на
    # каждой реплике, это давало спам в личный чат админа, хотя дебаг-канал
    # для него не заводили. alfred_tool_call — только явный opt-in по
    # точному имени, вайлдкард на него не распространяется.
    for sub in book.all():
        if not sub.broken and EVENT_ALFRED_TOOL_CALL in sub.event_types:
            await notifier.send_direct(sub.chat_id, text, reply_markup=markup)
