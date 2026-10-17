"""TelegramEventDispatcher — доставка событий здоровья в Telegram-чаты.

Рендер текста и фан-аут по подпискам живут здесь, а не в job'ах: job отдаёт
доменное событие, диспетчер решает, кому и как. message_id «перегрева»
запоминается, чтобы «остыл» ушёл reply'ем на него.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sa_home_bot.bot.notifier import Notifier, broadcast
from sa_home_bot.db.store import NOTIF_ALERT, NOTIF_CLEARED, OUTBOX_KIND_ALERT, Store
from sa_home_bot.domain.host import HostEvent
from sa_home_bot.domain.models import Event, SmartChange
from sa_home_bot.domain.render import render_event, render_host_event, render_smart_change
from sa_home_bot.jobs.base import DispatchResult
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)


class TelegramEventDispatcher:
    def __init__(
        self,
        notifier: Notifier,
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
            message_id = await self._notifier.send_direct(
                sub.chat_id,
                text,
                outbox_kind=OUTBOX_KIND_ALERT,
                dedup_key=f"health:{event.component_id}",
            )
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
            # Тот же dedup_key, что у алерта: если алерт ещё ждёт в outbox,
            # свежий статус «восстановлено» заменяет его, а не шлётся вдогонку.
            message_id = await self._notifier.send_direct(
                sub.chat_id,
                text,
                reply_to_message_id=reply_to,
                outbox_kind=OUTBOX_KIND_ALERT,
                dedup_key=f"health:{event.component_id}",
            )
            if message_id is not None:
                delivered = True
                await self._store.record_notification(
                    event.component_id, sub.chat_id, NOTIF_CLEARED, message_id, now
                )
        return DispatchResult(delivered=delivered, handled=True)

    async def _fanout_without_outbox(
        self, subs: list[Subscription], text: str
    ) -> DispatchResult:
        delivered = await broadcast(subs, self._notifier, text) > 0
        return DispatchResult(delivered=delivered, handled=delivered or not subs)

    async def _dispatch_host(self, event: HostEvent) -> DispatchResult:
        subs = self._subscriptions.accepting(event.type)
        return await self._fanout_without_outbox(subs, render_host_event(event))

    async def dispatch_host_alert(self, event: HostEvent) -> DispatchResult:
        return await self._dispatch_host(event)

    async def dispatch_host_clear(self, event: HostEvent) -> DispatchResult:
        return await self._dispatch_host(event)

    async def dispatch_smart(self, change: SmartChange) -> DispatchResult:
        subs = self._subscriptions.accepting(change.event_type)
        return await self._fanout_without_outbox(subs, render_smart_change(change))
