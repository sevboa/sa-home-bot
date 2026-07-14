"""Приём событий сервиса node: ``node_joined`` (рой пополнился),
``update_finished`` (самообновление ноды завершилось — файлы на диске
обновлены, процесс НЕ перезапущен, это делает человек через restart_node).

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
EVENT_UPDATE_FINISHED = "update_finished"


def render_node_joined(node_id: str, endpoint: str) -> str:
    return f"🕸 К рою присоединилась нода «{node_id}» ({endpoint})."


def render_update_finished(node_id: str, ok: bool, version: str | None, error: str | None) -> str:
    if ok:
        return (
            f"⬆️ Нода «{node_id}» обновлена до v{version} — "
            f"нужен перезапуск (nodectl restart_node)."
        )
    return f"⚠️ Обновление ноды «{node_id}» не удалось: {error}"


def build_node_event_handler(book: SubscriptionBook, notifier: Notifier):
    """Callback для ServiceLink(node).on_event."""

    async def handle(env: Envelope) -> None:
        name = env.payload.get("event")
        data = env.payload.get("data", {})
        if name == EVENT_NODE_JOINED:
            node_id = data.get("node_id")
            if not node_id:
                return
            text = render_node_joined(node_id, data.get("endpoint") or "?")
        elif name == EVENT_UPDATE_FINISHED:
            # Событие описывает саму себя — src это и есть обновившаяся
            # нода (в отличие от node_joined, где src — сосед, а объект
            # события — третья нода); работает и для пиров — ретрансляция
            # событий уже устроена (см. node/app.py:_relay_peer_event).
            if env.src is None or not env.src.node:
                return
            text = render_update_finished(
                env.src.node, bool(data.get("ok")), data.get("version"), data.get("error")
            )
        else:
            return
        await broadcast_system(book, notifier, text)

    return handle
