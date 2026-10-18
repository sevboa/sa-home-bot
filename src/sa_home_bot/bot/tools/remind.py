from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec
from sa_home_bot.config import reminder_reason
from sa_home_bot.llm.prompt import wrap_system_directive
from sa_home_bot.proto.addresses import (
    LLM_NODE,
    LLM_SERVICE,
    TASKS_ADDRESS,
)
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.tasks import protocol as task_protocol
from sa_home_bot.utils.clock import utc_now

EVENT_UPDATE_FINISHED = "update_finished"
EVENT_RESTART_APPLIED = "restart_applied"

RESTART_EVENT_FALLBACK_S = 300.0


_AFTER_EVENT_TYPES = (EVENT_RESTART_APPLIED, EVENT_UPDATE_FINISHED)


def _close_pending_tool_calls(
    history: list[dict[str, Any]], self_result: str | None = None
) -> list[dict[str, Any]]:
    for i in range(len(history) - 1, -1, -1):
        msg = history[i]
        if msg.get("role") == "tool":
            continue
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            return history
        pending = msg["tool_calls"][len(history) - i - 1 :]
        snapshot = list(history)
        for idx, call in enumerate(pending):
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            content = "(результат не сохранён)"
            if idx == 0 and self_result is not None:
                content = self_result
            snapshot.append({"role": "tool", "content": content, "name": fn.get("name", "")})
        return snapshot
    return history


async def tool_remind(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.chat_id is None or ctx.dialogue_id is None or ctx.trigger_message_id is None:
        return "ошибка: отложенные задачи недоступны вне диалога"
    if ctx.node_link is None:
        return "ошибка: служба задач недоступна"
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return "ошибка: не указано, что сделать/сказать (text)"

    after_event = args.get("after_event")
    event_node: str | None = None
    event_type: str | None = None
    if after_event is not None:
        if not isinstance(after_event, dict):
            return "ошибка: after_event должен быть объектом {node, event}"
        event_node = str(after_event.get("node") or "").strip()
        event_type = str(after_event.get("event") or "").strip()
        if not event_node or event_type not in _AFTER_EVENT_TYPES:
            return (
                "ошибка: after_event.node обязателен, after_event.event — одно из "
                + ", ".join(_AFTER_EVENT_TYPES)
            )
        if ctx.woken_by == (event_node, event_type):
            return (
                f"ошибка: событие «{event_type}» от «{event_node}» уже наступило — "
                "именно из-за него тебя сейчас разбудили. Ждать его снова нельзя "
                "(зациклишься) — вызови действие, которое тебя просили сделать, "
                "или честно сообщи, что не получилось."
            )

    when_raw = args.get("when")
    if when_raw is None and after_event is None:
        return "ошибка: не указано время (when, ISO 8601)"
    if when_raw is not None:
        if not isinstance(when_raw, str) or not when_raw.strip():
            return "ошибка: не указано время (when, ISO 8601)"
        try:
            due_at = datetime.fromisoformat(when_raw)
        except ValueError:
            return "ошибка: 'when' должен быть в формате ISO 8601, например 2026-07-24T21:30:00"
        if due_at.tzinfo is None:
            due_at = due_at.astimezone()
        due_at_utc = due_at.astimezone(UTC)
        if due_at_utc <= utc_now():
            return "ошибка: указанное время уже прошло"
    else:
        due_at_utc = utc_now() + timedelta(seconds=RESTART_EVENT_FALLBACK_S)
        due_at = due_at_utc.astimezone()

    if after_event is not None:
        directive = (
            f"Ты ждал(а) событие «{event_type}» от ноды «{event_node}», после "
            f"чего тебя попросили сделать вот что: «{text.strip()}». Прежде чем "
            "продолжать, сверь реальное состояние (например, node_manage/"
            "check_update) — событие могло не прийти вовсе, тогда честно скажи, "
            "что не подтвердилось, не выдавай желаемое за случившееся. Просто "
            "вызови нужный тул — время сейчас проверять не нужно. НЕ зови "
            f"remind(after_event={{\"node\": \"{event_node}\", \"event\": "
            f'"{event_type}"}}) снова — это то самое событие, по которому тебя '
            "только что разбудили, ставить его ожидание заново — зациклиться."
        )
    else:
        directive = (
            f"Настало время ({due_at:%Y-%m-%d %H:%M}), на которое тебя раньше "
            f"попросили сделать вот что: «{text.strip()}». Сделай/скажи это "
            "сейчас, от своего имени, в характере — как будто сам вспомнил, а не "
            "отвечаешь на прямой вопрос. Если нужно что-то посчитать или узнать "
            "(погоду, курс) — пользуйся инструментами, не полагайся на память."
        )
    history = _close_pending_tool_calls(ctx.history, self_result=args.get("_self_result"))
    task_args = {
        "messages": [*history, wrap_system_directive(directive)],
        "reason": reminder_reason(ctx.settings.llm),
        "chat_id": ctx.chat_id,
    }
    meta = {
        "kind": task_protocol.TASK_KIND_LLM_CHAT,
        "chat_id": ctx.chat_id,
        "dialogue_id": ctx.dialogue_id,
        "trigger_message_id": ctx.trigger_message_id,
        "message_thread_id": ctx.message_thread_id,
    }
    if after_event is not None:
        meta["awaited_node"] = event_node
        meta["awaited_event"] = event_type
    dst = TASKS_ADDRESS
    create_args: dict[str, Any] = {
        "due_at": due_at_utc.isoformat(),
        "dst_node": LLM_NODE,
        "dst_service": LLM_SERVICE,
        "action": task_protocol.ACTION_CHAT_LOOP,
        "args": task_args,
        "timeout_s": ctx.settings.llm.request_timeout_s,
        "meta": meta,
    }
    if after_event is not None:
        create_args["await_event"] = {"node": event_node, "event_type": event_type}
    try:
        await ctx.node_link.command(task_protocol.ACTION_CREATE, create_args, dst=dst)
    except (ServiceUnavailableError, ProtoError) as exc:
        return f"внутренняя ошибка: не удалось поставить задачу ({exc})"

    if after_event is not None:
        return (
            f"жду событие «{event_type}» от «{event_node}», страховочный срок — "
            f"{due_at.strftime('%H:%M')} (местное время)"
        )
    return f"задача поставлена на {due_at.strftime('%Y-%m-%d %H:%M')} (местное время)"


async def schedule_agent_dialogue(
    node_link: ServiceLink,
    chat_id: int,
    messages: list[dict[str, Any]],
    reason: str,
    timeout_s: float,
    *,
    meta_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    due_at_utc = utc_now()
    dst = TASKS_ADDRESS
    create_args: dict[str, Any] = {
        "due_at": due_at_utc.isoformat(),
        "dst_node": LLM_NODE,
        "dst_service": LLM_SERVICE,
        "action": task_protocol.ACTION_CHAT_LOOP,
        "args": {"messages": messages, "reason": reason, "chat_id": chat_id},
        "timeout_s": timeout_s,
        "meta": {
            "kind": task_protocol.TASK_KIND_LLM_CHAT,
            "chat_id": chat_id,
            **(meta_extra or {}),
        },
    }
    return await node_link.command(task_protocol.ACTION_CREATE, create_args, dst=dst)


_DECL_REMIND: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "remind",
        "description": (
            "Поставить отложенную задачу в этом же чате — НЕ готовый текст, а "
            "то, что нужно СДЕЛАТЬ или СКАЗАТЬ, когда придёт время: тогда тебя "
            "вызовут заново и ты сам сформулируешь ответ, при необходимости "
            "пользуясь другими инструментами (например, посмотреть погоду "
            "именно в тот момент, а не сейчас). Ровно ОДНО из двух — when ИЛИ "
            "after_event. when — переведи то, что попросил пользователь "
            "('через 20 минут', 'завтра в 9 утра'), в точную дату-время сам, "
            "используя текущее время из контекста разговора. after_event — "
            "проснуться по событию ноды, а не по времени: НЕ сиди и не "
            "жди/не переспрашивай состояние сам, а поставь задачу "
            "проснуться, когда нода реально подтвердит нужное (страховочный "
            "срок на случай, если событие не придёт, считается сам, без "
            "when). Для update/restart_node ноды (node_manage) это НЕ "
            "нужно — они уже сами ставят такое ожидание, вызывай remind "
            "только для СВОИХ похожих случаев ожидания события."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": "Точная дата-время в ISO 8601, например 2026-07-24T21:30:00",
                },
                "after_event": {
                    "type": "object",
                    "description": (
                        "Проснуться по событию ноды, а не по времени — для "
                        "node_manage(restart_node)/update, когда нужно дождаться "
                        "реального результата, не выдумывая его заранее."
                    ),
                    "properties": {
                        "node": {
                            "type": "string",
                            "description": "Имя ноды, чьего события ждать (например: arch-t480)",
                        },
                        "event": {
                            "type": "string",
                            "enum": list(_AFTER_EVENT_TYPES),
                            "description": (
                                "restart_applied — нода реально перезапустилась на новой "
                                "версии (после restart_node); update_finished — файлы "
                                "обновления легли на диск (после update, ДО рестарта)."
                            ),
                        },
                    },
                    "required": ["node", "event"],
                },
                "text": {
                    "type": "string",
                    "description": "Что нужно сделать или сказать в момент срабатывания",
                },
            },
            "required": ["text"],
        },
    },
}


REMIND = ToolSpec(
    name="remind", handler=tool_remind, declaration=_DECL_REMIND, telegram_only=True
)
