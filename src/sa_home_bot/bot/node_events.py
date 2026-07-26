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
ai_turns делаются здесь, по meta, которую сама служба tasks не читает),
``tool_call`` (та же служба tasks — факт вызова инструмента моделью внутри
уже сработавшей chat_loop-задачи, self-scheduled remind; только имя тула,
без аргументов/результата, см. tasks/protocol.py::EVENT_TOOL_CALL).

``node_joined``/``update_finished`` — тип в чат ``system`` (тот же канал,
что старт/останов, `bot/lifecycle.py`), рассылаются всем подпискам.
``llm_idle_sleep``/``llm_service_restart``/``task_prewake``/``task_result``
— адресно, только в конкретный chat_id из данных события (не через
event_types подписки: адрес уже точный, дублировать его подписками
незачем). ``tool_call`` — через подписки с ``event_types=["alfred_tool_call"]``
(`bot/lifecycle.py::notify_tool_call`) — тот же путь, что и для живого /ai
(bot/ai_flow.py::request_alfred), адресата в самом событии нет. Остальные
события ноды (``service_started``/``service_failed`` и т.п.) сюда не
заведены — отдельная функциональность, вне рамок этого модуля.
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
from sa_home_bot.bot.lifecycle import broadcast_system, notify_tool_call
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Envelope
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.tasks import protocol as task_protocol

log = logging.getLogger(__name__)

EVENT_NODE_JOINED = "node_joined"
EVENT_NODE_DOWN = "node_down"
EVENT_NODE_UP = "node_up"
EVENT_UPDATE_FINISHED = "update_finished"
EVENT_SINGLETON_ACTIVATED = "singleton_activated"
EVENT_SINGLETON_YIELDED = "singleton_yielded"
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


def _human_downtime(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def render_node_down(node_id: str, down_s: float) -> str:
    return (
        f"🔌 Нода «{node_id}» не отвечает уже {_human_downtime(down_s)} — "
        f"а эта машина должна быть в сети всегда."
    )


def render_node_up(node_id: str) -> str:
    return f"✅ Нода «{node_id}» снова на связи."


def render_singleton_activated(slot: str, node_id: str) -> str:
    return f"🎛 Службу «{slot}» приняла нода «{node_id}»."


def render_singleton_yielded(slot: str, node_id: str) -> str:
    return f"🎛 Нода «{node_id}» уступила службу «{slot}» — вернулась основная."


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
        if name == task_protocol.EVENT_TOOL_CALL:
            await notify_tool_call(book, notifier, data.get("name", "?"))
            return
        if name == EVENT_NODE_JOINED:
            node_id = data.get("node_id")
            if not node_id:
                return
            text = render_node_joined(node_id, data.get("endpoint") or "?")
        elif name in (EVENT_NODE_DOWN, EVENT_NODE_UP):
            # Объект события — пропавшая нода из payload, а не src (объявляет
            # тот сосед, что её видит; см. node/watch.py::_is_announcer).
            node_id = data.get("node")
            if not node_id:
                return
            text = (
                render_node_down(node_id, float(data.get("down_s") or 0))
                if name == EVENT_NODE_DOWN
                else render_node_up(node_id)
            )
        elif name in (EVENT_SINGLETON_ACTIVATED, EVENT_SINGLETON_YIELDED):
            slot, node_id = data.get("slot"), data.get("node")
            if not slot or not node_id:
                return
            # Переезд бота между нодами сообщает уже поднявшийся экземпляр —
            # тот, что уступил, к этому моменту закрывает сессию.
            text = (
                render_singleton_activated(slot, node_id)
                if name == EVENT_SINGLETON_ACTIVATED
                else render_singleton_yielded(slot, node_id)
            )
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
