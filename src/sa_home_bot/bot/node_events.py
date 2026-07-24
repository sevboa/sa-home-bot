"""Приём событий сервиса node: ``node_joined`` (рой пополнился),
``update_finished`` (самообновление ноды завершилось — файлы на диске
обновлены, процесс НЕ перезапущен, это делает человек через restart_node),
``llm_idle_sleep`` (служба llm сама погасила контейнер по простою —
llm/service.py, ретранслируется сюда тем же механизмом, что и события
пиров, см. node/app.py::build_router — локальным службам тоже включён
on_event), ``llm_service_restart`` (сам процесс службы llm останавливается
— деплой/апдейт/ручной restart, см. llm/app.py::run_llm/LlmService.
notify_restart — тот же список активных chat_id, что и у idle_sleep),
``task_prewake``/``task_result`` (служба tasks — отложенные задачи роя, см.
sa_home_bot.tasks; у той нет доступа к Telegram, доставка и запись в
ai_turns делаются здесь, по meta, которую сама служба tasks не читает).

``node_joined``/``update_finished`` — тип в чат ``system`` (тот же канал,
что старт/останов, `bot/lifecycle.py`), рассылаются всем подпискам.
``llm_idle_sleep``/``llm_service_restart``/``task_prewake``/``task_result``
— адресно, только в конкретный chat_id из данных события (не через
event_types подписки: адрес уже точный, дублировать его подписками
незачем). Остальные события ноды (``service_started``/``service_failed`` и
т.п.) сюда не заведены — отдельная функциональность, вне рамок этого
модуля.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime

from sa_home_bot.bot.ai_flow import (
    ALBERT_ASLEEP,
    ALBERT_TASK_MISSED,
    ALBERT_UNAVAILABLE,
    ARNOLD_WAKING,
    CLOSING_TEXT,
    RESTART_TEXT,
    STEPS_TEXT,
)
from sa_home_bot.bot.lifecycle import broadcast_system
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Envelope
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

log = logging.getLogger(__name__)

EVENT_NODE_JOINED = "node_joined"
EVENT_UPDATE_FINISHED = "update_finished"
# Строковые литералы, не импорт из llm/service.py — та же конвенция, что и
# для событий выше (это не "источник правды", просто совпадающая строка;
# импорт бота из пакета llm ради одной константы того не стоит).
EVENT_LLM_IDLE_SLEEP = "llm_idle_sleep"
EVENT_LLM_SERVICE_RESTART = "llm_service_restart"


def _format_alfred_reply(raw: str) -> str:
    return f"<b>Альфред:</b> {html.escape(raw.strip())}"


async def _handle_task_prewake(notifier: Notifier, data: dict) -> None:
    meta = data.get("meta") or {}
    if meta.get("kind") != task_protocol.TASK_KIND_LLM_CHAT:
        return  # незнакомый вид задачи — доставлять/показывать нечего
    chat_id = meta.get("chat_id")
    if chat_id is None:
        return
    status = data.get("status")
    if status == "waking":
        await notifier.send_direct(chat_id, STEPS_TEXT)
    elif status == "ready":
        await notifier.send_direct(chat_id, ARNOLD_WAKING)
    elif status == "failed":
        text = ALBERT_UNAVAILABLE if data.get("reason") == "unreachable" else ALBERT_ASLEEP
        await notifier.send_direct(chat_id, text)


async def _handle_task_result(notifier: Notifier, store: Store, data: dict) -> None:
    meta = data.get("meta") or {}
    if meta.get("kind") != task_protocol.TASK_KIND_LLM_CHAT:
        return
    chat_id = meta.get("chat_id")
    if chat_id is None:
        return
    trigger_message_id = meta.get("trigger_message_id")
    if not data.get("ok"):
        await notifier.send_direct(
            chat_id, ALBERT_TASK_MISSED, reply_to_message_id=trigger_message_id
        )
        return
    raw = (data.get("result") or {}).get("response", "")
    sent_id = await notifier.send_direct(
        chat_id, _format_alfred_reply(raw), reply_to_message_id=trigger_message_id
    )
    dialogue_id = meta.get("dialogue_id")
    if sent_id is not None and dialogue_id is not None:
        await store.record_ai_turn(
            chat_id, sent_id, dialogue_id, "assistant", raw, datetime.now(tz=UTC)
        )


def render_node_joined(node_id: str, endpoint: str) -> str:
    return f"🕸 К рою присоединилась нода «{node_id}» ({endpoint})."


def render_update_finished(node_id: str, ok: bool, version: str | None, error: str | None) -> str:
    if ok:
        return (
            f"⬆️ Нода «{node_id}» обновлена до v{version} — "
            f"нужен перезапуск (nodectl restart_node)."
        )
    return f"⚠️ Обновление ноды «{node_id}» не удалось: {error}"


def build_node_event_handler(book: SubscriptionBook, notifier: Notifier, store: Store):
    """Callback для ServiceLink(node).on_event."""

    async def handle(env: Envelope) -> None:
        name = env.payload.get("event")
        data = env.payload.get("data", {})
        if name == task_protocol.EVENT_TASK_PREWAKE:
            await _handle_task_prewake(notifier, data)
            return
        if name == task_protocol.EVENT_TASK_RESULT:
            await _handle_task_result(notifier, store, data)
            return
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
        elif name == EVENT_LLM_SERVICE_RESTART:
            for chat_id in data.get("chat_ids", []):
                await notifier.send_direct(chat_id, RESTART_TEXT)
            return
        else:
            return
        await broadcast_system(book, notifier, text)

    return handle
