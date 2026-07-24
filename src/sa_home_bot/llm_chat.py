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


async def run_chat_loop(
    node_link: ServiceLink,
    dst: Address,
    timeout: float,
    messages: list[dict[str, Any]],
    tool_ctx: ai_tools.ToolContext,
    think: bool,
    telegram_chat_id: int | None,
    log_chat_id: Any,
) -> str:
    """Один проход диалога с моделью: раунды tool-calling (до
    MAX_TOOL_ROUNDS), пока не придёт финальный текст.

    ``messages`` мутируется по ходу (дописываются tool_calls/результаты) —
    вызывающий передаёт отдельный список на каждый проход, если хочет
    сохранить исходную историю чистой. ``tool_ctx.history`` привязывается к
    этому же списку (та же ссылка) — тул remind видит в нём ровно то, что
    сейчас видит модель (включая уже случившиеся раунды tool-calling), не
    отдельный запрос к БД (у службы tasks её и нет)."""
    tool_ctx.history = messages
    for _round in range(MAX_TOOL_ROUNDS):
        args: dict[str, Any] = {
            "messages": messages,
            "tools": ai_tools.TOOL_DECLARATIONS,
            "think": think,
        }
        if telegram_chat_id is not None:
            # chat_id — не для маршрутизации (та по dst), а чтобы служба
            # llm знала, какие чаты уведомлять при llm_idle_sleep.
            args["chat_id"] = telegram_chat_id
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
            messages.append({"role": "tool", "content": tool_result, "name": name})
    # Лимит раундов исчерпан — модель зациклилась на вызовах инструментов,
    # не дав финального текста.
    raise ProtoError(ERR_INTERNAL, "превышен лимит раундов tool-calling")
