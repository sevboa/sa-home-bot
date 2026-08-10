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

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ERR_INTERNAL, Address, ProtoError

log = logging.getLogger(__name__)

ACTION_CHAT = "chat"
# Дублирует llm/service.py::ACTION_CHAT_PROGRESS — тот же приём, что и с
# ACTION_CHAT выше: этот модуль намеренно не импортирует llm/service.py
# (используется службой tasks, у которой нет тяжёлых зависимостей LLM-
# службы, только клиентский ServiceLink).
ACTION_CHAT_PROGRESS = "chat_progress"
# Опрос буфера частичной генерации (этап 34, Фаза 2 — Rich-стрим ответов):
# раз в ~1с, короткий таймаут на сам round-trip (не путать с общим timeout
# на весь ответ модели, который ждёт вызывающий).
_POLL_INTERVAL_S = 1.0
_POLL_TIMEOUT_S = 3.0

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

# (имя тула, аргументы) — вызывается ПЕРЕД хендлером тула (в отличие от
# ToolCallSink выше, который зовётся после, с результатом — для durable-
# записи). Живая находка 2026-08-10: пользователь захотел, чтобы статусная
# фраза ("Альфред проверяет погоду...") появлялась, пока тул реально
# выполняется, а не постфактум — для медленных тулов (web_search, torrents)
# постфактум-уведомление не даёт ощущения "происходит прямо сейчас".
ToolStartSink = Callable[[str, dict[str, Any]], Awaitable[None]]

# Ремарка «Логопеда» (llm/speech_therapy.py) на финальный текстовый ответ —
# отдельно от response, чтобы вызывающий мог отправить её отдельным
# сообщением ПОСЛЕ ответа Альфреда (решение пользователя 2026-08-03: раньше
# ремарка дописывалась в тот же текст и уезжала одним сообщением, к тому же
# ломано отформатированным — см. bot/ai_flow.py::request_alfred).
SpeechRemarkSink = Callable[[str], Awaitable[None]]

# Кусок накопленного (не дельта) текста текущего раунда + флаг "раунд
# завершён" — этап 34, Фаза 2. Вызывается на каждый тик опроса
# chat_progress, ПОКА идёт конкретный раунд tool-calling; раунд, который
# оказался tool-calling-раундом (не финальным), просто не попадёт в
# персистентный rich-ответ — это решает вызывающий (bot/rich_stream.py),
# не этот модуль.
PartialSink = Callable[[str, bool], Awaitable[None]]


async def _poll_partial(
    node_link: ServiceLink,
    dst: Address,
    request_id: str,
    on_partial: PartialSink,
) -> None:
    """Раз в ~1с опрашивать llm.chat_progress, пока раунд не завершится —
    тот же паттерн опроса, что wake_core.py::wait_for_service/fetch_state,
    только вместо get_state зовём command с параметром: get_state параметров
    не принимает (PROTOCOL.md), а Альфред может вести несколько диалогов
    одновременно (разные чаты семьи) — частичный текст держится по
    request_id, не одним полем состояния службы.

    Финальный текст ответа вызывающий всё равно берёт из результата
    основного ``command(ACTION_CHAT, ...)``, не отсюда — сбой опроса не
    критичен, в худшем случае превью отстаёт от реальной генерации на
    несколько тиков. Останавливается сама на ``done=True`` от службы или по
    отмене задачи вызывающим (когда основной command() уже резолвился)."""
    while True:
        await asyncio.sleep(_POLL_INTERVAL_S)
        try:
            state = await node_link.command(
                ACTION_CHAT_PROGRESS,
                {"request_id": request_id},
                dst=dst,
                timeout=_POLL_TIMEOUT_S,
            )
        except (ServiceUnavailableError, ProtoError, TimeoutError):
            continue
        done = bool(state.get("done"))
        await on_partial(state.get("partial", ""), done)
        if done:
            return


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
    on_partial: PartialSink | None = None,
    on_tool_start: ToolStartSink | None = None,
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
    думать", не мешать ей явным флагом. См. llm/ollama.py::chat().

    ``on_partial`` — этап 34, Фаза 2 (Rich-стрим ответов): не передан —
    поведение не меняется вовсе (обычный блокирующий command(), так вызывает
    служба tasks). Передан — каждый раунд идёт со своим request_id и
    параллельным опросом chat_progress (см. _poll_partial выше), колбэк
    зовётся на каждый тик текущего раунда; какой из раундов в итоге
    персистится как настоящий ответ — решает вызывающий, не этот цикл.

    ``on_tool_start`` — уведомление ДО выполнения тула (имя, аргументы),
    в отличие от ``on_tool_call`` (после, с результатом — durable-запись).
    Чистый side-effect, ничего не блокирует и не меняет; не зовётся для
    неизвестного модели тула (там и выполнять нечего)."""
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

    async def _call_chat(args: dict[str, Any]) -> dict[str, Any]:
        """Один раунд chat: без on_partial — просто command(), как раньше.
        С on_partial — плюс параллельная задача опроса chat_progress на
        время этого конкретного раунда (см. _poll_partial)."""
        if on_partial is None:
            return await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
        request_id = uuid.uuid4().hex
        streamed_args = {**args, "request_id": request_id}
        poll_task = asyncio.create_task(_poll_partial(node_link, dst, request_id, on_partial))
        try:
            return await node_link.command(ACTION_CHAT, streamed_args, dst=dst, timeout=timeout)
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

    for _round in range(MAX_TOOL_ROUNDS):
        args = _chat_args(toolkit.declarations)
        result = await _call_chat(args)
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
                if on_tool_start is not None:
                    await on_tool_start(name, call_args)
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
    result = await _call_chat(_chat_args([]))
    response = result.get("response", "")
    if response:
        await _maybe_send_remark(result)
        return response
    # Пустой ответ без единого доступного инструмента — это уже не
    # «зациклилась», а настоящий сбой генерации: сюда и правда нужен HICCUP.
    raise ProtoError(ERR_INTERNAL, "модель не дала ответа после лимита раундов")
