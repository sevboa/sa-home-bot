"""Приём событий сервиса node: ``node_joined`` (рой пополнился),
``update_finished`` (самообновление ноды завершилось — файлы на диске
обновлены, процесс НЕ перезапущен, это делает человек через restart_node),
``llm_idle_sleep`` (служба llm сама погасила контейнер по простою —
llm/service.py, ретранслируется сюда тем же механизмом, что и события
пиров, см. node/app.py::build_router — локальным службам тоже включён
on_event).

``node_joined``/``update_finished`` — тип в чат ``system`` (тот же канал,
что старт/останов, `bot/lifecycle.py`), рассылаются всем подпискам.
``llm_idle_sleep`` — адресно, только в перечисленные в событии chat_id (не
через event_types подписки: список чатов уже точный, дублировать его
подписками незачем). Остальные события ноды (``service_started``/
``service_failed`` и т.п.) сюда не заведены — отдельная функциональность,
вне рамок этого модуля.
"""

from __future__ import annotations

import logging

from sa_home_bot.bot.ai_flow import CLOSING_TEXT
from sa_home_bot.bot.lifecycle import broadcast_system
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.proto.messages import Envelope
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)

EVENT_NODE_JOINED = "node_joined"
EVENT_UPDATE_FINISHED = "update_finished"
# Строковый литерал, не импорт из llm/service.py — та же конвенция, что и
# для событий выше (это не "источник правды", просто совпадающая строка;
# импорт бота из пакета llm ради одной константы того не стоит).
EVENT_LLM_IDLE_SLEEP = "llm_idle_sleep"


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
        elif name == EVENT_LLM_IDLE_SLEEP:
            # Адресно — только в перечисленные chat_id, не всем подпискам
            # (не через broadcast_system): служба сама знает точный список
            # чатов, где были запросы за это тёплое окно (llm/service.py).
            for chat_id in data.get("chat_ids", []):
                await notifier.send_direct(chat_id, CLOSING_TEXT)
            return
        else:
            return
        await broadcast_system(book, notifier, text)

    return handle
