"""Приём событий монитора: proto-событие → доменная модель → рассылка в чаты.

Подписки, авторизация и рендер остаются в боте: монитор шлёт голые данные,
здесь они превращаются в Event/SmartChange и уходят через
TelegramEventDispatcher (reply-цепочка «остыл» → «перегрев» продолжает
работать — message_id хранятся в БД бота).
"""

from __future__ import annotations

import logging

from sa_home_bot.bot.dispatch import TelegramEventDispatcher
from sa_home_bot.bot.monitor_state import parse_overheat_event, parse_smart_change
from sa_home_bot.domain.models import (
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    EVENT_SMART_DEGRADED,
    EVENT_SMART_RECOVERED,
)
from sa_home_bot.proto.messages import Envelope

log = logging.getLogger(__name__)


def build_event_handler(dispatcher: TelegramEventDispatcher):
    """Callback для ProtoClient.on_event: разбирает и рассылает события."""

    async def handle(env: Envelope) -> None:
        name = env.payload.get("event")
        data = env.payload.get("data", {})
        try:
            if name == EVENT_OVERHEAT_STARTED:
                await dispatcher.dispatch_alert(parse_overheat_event(name, data))
            elif name == EVENT_OVERHEAT_CLEARED:
                await dispatcher.dispatch_clear(parse_overheat_event(name, data))
            elif name in (EVENT_SMART_DEGRADED, EVENT_SMART_RECOVERED):
                await dispatcher.dispatch_smart(parse_smart_change(name, data))
            else:
                log.info("Событие монитора без обработчика: %s", name)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("Невалидное событие монитора %r: %s", name, exc)

    return handle
