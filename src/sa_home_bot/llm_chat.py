"""Общий цикл tool-calling поверх llm.chat (LLM_INTEGRATION_PLAN.md §7.1).

Раньше жил только в bot/ai_flow.py (живой /ai) — вынесен сюда (живая
находка 2026-07-24, генерализация напоминаний в отдельный сервис задач),
потому что теперь его использует и служба tasks (bot/ai_flow.py остаётся
для живого /ai, sa_home_bot.tasks.service — для отложенных задач вида
"спросить нейронку", в т.ч. созданных самой моделью через тул remind).

Зависит от bot.tools (ToolContext/TOOL_HANDLERS) — это чистый Python без
aiogram (см. докстринг ToolContext), поэтому служба tasks может его
импортировать, не таща Telegram-зависимости.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.proto.messages import ERR_INTERNAL, Address, ProtoError

log = logging.getLogger(__name__)

ACTION_CHAT = "chat"

# Сколько раз подряд можно уйти в tool_calls, прежде чем модель обязана дать
# финальный текстовый ответ — защита от зацикливания (LLM_INTEGRATION_
# PLAN.md §7.1 п.5).
MAX_TOOL_ROUNDS = 4

# (имя тула, аргументы, результат) — вызывается после каждого тула. Этот
# модуль сам БД не трогает (см. докстринг модуля — им пользуется и служба
# tasks, у которой БД бота нет вовсе); колбэк передаёт вызывающий (живой
# /ai в bot/ai_flow.py пишет в Store.record_tool_call, tasks его просто не
# передаёт — см. schema.sql::ai_tool_calls).
ToolCallSink = Callable[[str, dict[str, Any], str], Awaitable[None]]


async def run_chat_loop(
    node_link: ServiceLink,
    dst: Address,
    timeout: float,
    messages: list[dict[str, Any]],
    tool_ctx: ai_tools.ToolContext,
    think: bool | None,
    telegram_chat_id: int | None,
    log_chat_id: Any,
    on_tool_call: ToolCallSink | None = None,
    role: str | None = None,
) -> str:
    """Один проход диалога с моделью: раунды tool-calling (до
    MAX_TOOL_ROUNDS), пока не придёт финальный текст.

    ``messages`` мутируется по ходу (дописываются tool_calls/результаты) —
    вызывающий передаёт отдельный список на каждый проход, если хочет
    сохранить исходную историю чистой. ``tool_ctx.history`` привязывается к
    этому же списку (та же ссылка) — тул remind видит в нём ровно то, что
    сейчас видит модель (включая уже случившиеся раунды tool-calling), не
    отдельный запрос к БД (у службы tasks её и нет).

    ``role`` — какой системный промпт использует служба llm (см.
    llm/service.py, llm/prompt.py): ``None``/не передан — персонаж Альфреда
    (по умолчанию, как было всегда), ``"router"`` — служебный триаж без
    персонажа (живая находка 2026-07-25: см. llm/prompt.py::
    ROUTER_SYSTEM_PROMPT про то, почему триаж выделен в отдельный вызов).

    ``think=None`` — не слать поле think вообще (живая находка 2026-07-25,
    решение пользователя): на qwen3.5/3.6 принудительный think=false
    ломал качество ответа (несуществующие даты, проигнорированный верный
    результат тула) — у этих моделей своя адаптивная логика "думать/не
    думать", не мешать ей явным флагом. См. llm/ollama.py::chat()."""
    tool_ctx.history = messages
    for _round in range(MAX_TOOL_ROUNDS):
        args: dict[str, Any] = {
            "messages": messages,
            "tools": ai_tools.TOOL_DECLARATIONS,
        }
        if think is not None:
            args["think"] = think
        if telegram_chat_id is not None:
            # chat_id — не для маршрутизации (та по dst), а чтобы служба
            # llm знала, какие чаты уведомлять при llm_idle_sleep.
            args["chat_id"] = telegram_chat_id
        if role is not None:
            args["role"] = role
        result = await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
        tool_calls = result.get("tool_calls")
        if not tool_calls:
            return result.get("response", "")
        messages.append({"role": "assistant", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name", "")
            call_args = fn.get("arguments") or {}
            handler = ai_tools.TOOL_HANDLERS.get(name)
            if handler is None:
                tool_result = f"неизвестный инструмент: {name}"
            else:
                try:
                    tool_result = await handler(tool_ctx, call_args)
                except Exception as exc:  # noqa: BLE001 — сбой тула не должен ронять диалог
                    log.exception("llm_chat: тул %s упал (chat=%s)", name, log_chat_id)
                    tool_result = f"внутренняя ошибка инструмента: {exc}"
            # Живая находка 2026-07-24: раньше в проде не было видно вообще,
            # вызывает ли модель тул и с чем — баг "получил не знаю пояс, но
            # всё равно придумал время" диагностировать было нечем, кроме
            # логов Ollama. on_tool_call — durable-версия для живого /ai
            # (см. ToolCallSink выше).
            log.info(
                "llm_chat: тул %s(%s) -> %s (chat=%s)", name, call_args, tool_result, log_chat_id
            )
            if on_tool_call is not None:
                await on_tool_call(name, call_args, tool_result)
            messages.append({"role": "tool", "content": tool_result, "name": name})
    # Лимит раундов исчерпан — модель зациклилась на вызовах инструментов,
    # не дав финального текста.
    raise ProtoError(ERR_INTERNAL, "превышен лимит раундов tool-calling")
