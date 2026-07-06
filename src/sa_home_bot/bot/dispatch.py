"""TelegramEventDispatcher — доставка событий здоровья в Telegram-чаты.

Рендер текста и фан-аут по подпискам живут здесь, а не в job'ах: job отдаёт
доменное событие, диспетчер решает, кому и как. message_id «перегрева»
запоминается, чтобы «остыл» ушёл reply'ем на него.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol

from sa_home_bot.db.store import NOTIF_ALERT, NOTIF_CLEARED, Store
from sa_home_bot.domain.models import Event, SmartChange
from sa_home_bot.domain.render import render_event, render_smart_change
from sa_home_bot.jobs.base import DispatchResult
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)


class NotifierProtocol(Protocol):
    async def send_direct(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> int | None:
        """Отправить сообщение. Вернуть message_id при успехе, иначе None."""
        ...


class TelegramEventDispatcher:
    def __init__(
        self,
        notifier: NotifierProtocol,
        subscriptions: SubscriptionBook,
        store: Store,
    ) -> None:
        self._notifier = notifier
        self._subscriptions = subscriptions
        self._store = store

    async def dispatch_alert(self, event: Event) -> DispatchResult:
        text = render_event(event)
        now = datetime.now(tz=UTC)
        delivered = False
        for sub in self._subscriptions.accepting(event.type):
            message_id = await self._notifier.send_direct(sub.chat_id, text)
            if message_id is not None:
                delivered = True
                await self._store.record_notification(
                    event.component_id, sub.chat_id, NOTIF_ALERT, message_id, now
                )
        # handled всегда: помечаем доставленным, даже если часть чатов не
        # ответила, — иначе следующий тик зашлёт дубль живым подписчикам.
        return DispatchResult(delivered=delivered, handled=True)

    async def dispatch_clear(self, event: Event) -> DispatchResult:
        text = render_event(event)
        now = datetime.now(tz=UTC)
        delivered = False
        for sub in self._subscriptions.accepting(event.type):
            reply_to = await self._store.get_alert_message_id(event.component_id, sub.chat_id)
            message_id = await self._notifier.send_direct(
                sub.chat_id, text, reply_to_message_id=reply_to
            )
            if message_id is not None:
                delivered = True
                await self._store.record_notification(
                    event.component_id, sub.chat_id, NOTIF_CLEARED, message_id, now
                )
        return DispatchResult(delivered=delivered, handled=True)

    async def dispatch_smart(self, change: SmartChange) -> DispatchResult:
        text = render_smart_change(change)
        subs = self._subscriptions.accepting(change.event_type)
        delivered = False
        for sub in subs:
            message_id = await self._notifier.send_direct(sub.chat_id, text)
            if message_id is not None:
                delivered = True
        # Подписчики есть, но никому не дошло — baseline двигать нельзя,
        # деградацию нужно повторить на следующем прогоне.
        return DispatchResult(delivered=delivered, handled=delivered or not subs)
