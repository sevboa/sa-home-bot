"""Общий цикл tool-calling поверх llm.chat (LLM_INTEGRATION_PLAN.md §7.1).

Раньше жил только в bot/ai_flow.py (живой /ai) — вынесен сюда (живая
находка 2026-07-24, генерализация напоминаний в отдельный сервис задач),
потому что теперь его использует и служба tasks (bot/ai_flow.py остаётся
для живого /ai, sa_home_bot.tasks.service — для отложенных задач вида
"спросить нейронку", в т.ч. созданных самой моделью через тул remind).

Зависит от bot.tools (ToolContext/tools_for) — это чистый Python без
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
#
# 5 (было 4, решение пользователя 2026-07-27): с появлением web_search модель
# на одном вопросе делает несколько поисков подряд, переформулируя запрос —
# на живом вопросе про Нидерланды ушло три раунда только на поиск, и лимита
# в 4 едва хватало.
MAX_TOOL_ROUNDS = 5

# (имя тула, аргументы, результат) — вызывается после каждого тула. Этот
# модуль сам БД не трогает (см. докстринг модуля — им пользуется и служба
# tasks, у которой БД бота нет вовсе); колбэк передаёт вызывающий (живой
# /ai в bot/ai_flow.py пишет в Store.record_tool_call, tasks его просто не
# передаёт — см. schema.sql::ai_tool_calls).
ToolCallSink = Callable[[str, dict[str, Any], str], Awaitable[None]]

# Ремарка «Логопеда» (llm/speech_therapy.py) на финальный текстовый ответ —
# отдельно от response, чтобы вызывающий мог отправить её отдельным
# сообщением ПОСЛЕ ответа Альфреда (решение пользователя 2026-08-03: раньше
# ремарка дописывалась в тот же текст и уезжала одним сообщением, к тому же
# ломано отформатированным — см. bot/ai_flow.py::request_alfred).
SpeechRemarkSink = Callable[[str], Awaitable[None]]


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
    on_speech_remark: SpeechRemarkSink | None = None,
    role: str | None = None,
) -> str:
    """Один проход диалога с моделью: раунды tool-calling (до
    MAX_TOOL_ROUNDS), пока не придёт финальный текст.

    Если лимит раундов исчерпан, а модель всё ещё зовёт инструменты, проход
    НЕ падает: делается ещё один запрос без деклараций инструментов — звать
    нечего, и модель формулирует ответ из уже собранного (см. конец функции).

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
    # Комплект собирается ОДИН раз на проход и по правам собеседника: тула, на
    # который у него нет прав, модель не видит вовсе (см. bot/tools.py::
    # tools_for — требование "Альфред не отказывает, а не умеет").
    toolkit = ai_tools.tools_for(tool_ctx.subscription)

    def _chat_args(tools: list[dict[str, Any]]) -> dict[str, Any]:
        args: dict[str, Any] = {"messages": messages, "tools": tools}
        if think is not None:
            args["think"] = think
        if telegram_chat_id is not None:
            # chat_id — не для маршрутизации (та по dst), а чтобы служба
            # llm знала, какие чаты уведомлять при llm_idle_sleep.
            args["chat_id"] = telegram_chat_id
        if role is not None:
            args["role"] = role
        return args

    async def _maybe_send_remark(result: dict[str, Any]) -> None:
        remark = result.get("speech_remark")
        if remark and on_speech_remark is not None:
            await on_speech_remark(remark)

    for _round in range(MAX_TOOL_ROUNDS):
        args = _chat_args(toolkit.declarations)
        result = await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
        tool_calls = result.get("tool_calls")
        if not tool_calls:
            await _maybe_send_remark(result)
            return result.get("response", "")
        messages.append({"role": "assistant", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name", "")
            call_args = fn.get("arguments") or {}
            # Тот же отфильтрованный комплект, что ушёл в декларации: если
            # модель выдумает имя тула, которого ей не давали, — сюда она не
            # пройдёт, права проверены один раз и в одном месте.
            handler = toolkit.handlers.get(name)
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
    # Лимит раундов исчерпан. Раньше здесь был ProtoError → пользователь
    # получал ALBERT_HICCUP («Альфред отвлёкся, повторите») после того, как
    # прождал несколько минут, — и это при том, что результаты инструментов
    # уже лежали в messages, отвечать было ЧЕМ. Живая находка 2026-07-27: на
    # вопросе про Нидерланды модель сделала три поиска подряд и упёрлась в
    # лимит буквально на последнем раунде.
    #
    # Решение пользователя: не ломаться, а дожать. Последний запрос идёт БЕЗ
    # деклараций инструментов — тогда модели просто нечего вызвать, и она
    # обязана сформулировать текст из того, что уже собрала.
    log.info(
        "llm_chat: лимит раундов (%d) исчерпан, дожимаем ответ без тулов (chat=%s)",
        MAX_TOOL_ROUNDS,
        log_chat_id,
    )
    result = await node_link.command(ACTION_CHAT, _chat_args([]), dst=dst, timeout=timeout)
    response = result.get("response", "")
    if response:
        await _maybe_send_remark(result)
        return response
    # Пустой ответ без единого доступного инструмента — это уже не
    # «зациклилась», а настоящий сбой генерации: сюда и правда нужен HICCUP.
    raise ProtoError(ERR_INTERNAL, "модель не дала ответа после лимита раундов")
