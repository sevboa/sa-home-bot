"""Приём событий сервиса node: сейчас — ``node_joined`` (рой пополнился).

Тип в чат — ``system`` (тот же канал, что старт/останов, `bot/lifecycle.py`).
Остальные события ноды (``service_started``/``service_failed`` и т.п.) сюда
не заведены — отдельная функциональность, вне рамок этого модуля.
"""

from __future__ import annotations

import logging

from sa_home_bot.bot.lifecycle import broadcast_system
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.proto.messages import Envelope
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)

EVENT_NODE_JOINED = "node_joined"


def render_node_joined(node_id: str, endpoint: str) -> str:
    return f"🕸 К рою присоединилась нода «{node_id}» ({endpoint})."


def build_node_event_handler(book: SubscriptionBook, notifier: Notifier):
    """Callback для ServiceLink(node).on_event."""

    async def handle(env: Envelope) -> None:
        name = env.payload.get("event")
        if name != EVENT_NODE_JOINED:
            return
        data = env.payload.get("data", {})
        node_id = data.get("node_id")
        if not node_id:
            return
        text = render_node_joined(node_id, data.get("endpoint") or "?")
        await broadcast_system(book, notifier, text)

    return handle
