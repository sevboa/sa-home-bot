from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import (
    ServiceLink,
    ServiceTimeoutError,
    ServiceUnavailableError,
)
from sa_home_bot.proto.messages import (
    ERR_INTERNAL,
    ERR_UNAVAILABLE,
    Address,
    ProtoError,
    new_id,
)
from sa_home_bot.utils.ids import short_token

log = logging.getLogger(__name__)

ACTION_CHAT = "chat"
ACTION_CHAT_PROGRESS = "chat_progress"
_POLL_INTERVAL_S = 1.0
_POLL_TIMEOUT_S = 3.0

CHAT_RETRY_DELAYS_S: tuple[float, ...] = (3.0, 6.0)


def _is_link_gap(exc: Exception) -> bool:
    if isinstance(exc, ServiceTimeoutError):
        return False
    if isinstance(exc, ServiceUnavailableError):
        return True
    return isinstance(exc, ProtoError) and exc.code == ERR_UNAVAILABLE


MAX_TOOL_ROUNDS = 5

_TOOL_RESULT_INLINE_LIMIT = 1000
_TOOL_RESULT_PREVIEW_CHARS = 600


def _inline_or_cache(tool_ctx: ai_tools.ToolContext, tool_result: str) -> str:
    if len(tool_result) <= _TOOL_RESULT_INLINE_LIMIT:
        return tool_result
    recall_id = short_token()
    tool_ctx.tool_result_cache[recall_id] = tool_result
    return (
        f"{tool_result[:_TOOL_RESULT_PREVIEW_CHARS]}\n"
        f"…[сокращено для экономии контекста, всего {len(tool_result)} симв.; "
        f'если этого превью не хватает — вызови recall_tool_result(id="{recall_id}")]'
    )


ToolCallSink = Callable[[str, dict[str, Any], str], Awaitable[None]]

ToolStartSink = Callable[[str, dict[str, Any]], Awaitable[None]]

SpeechRemarkSink = Callable[[str], Awaitable[None]]

@dataclass
class ChatStats:
    first_prompt_tokens: int | None = None
    max_prompt_tokens: int = 0
    eval_tokens: int = 0
    done_reason: str | None = None
    num_ctx: int | None = None
    rounds: int = 0

    def absorb(self, result: dict[str, Any]) -> None:
        self.rounds += 1
        prompt = result.get("prompt_eval_count")
        if isinstance(prompt, int) and not isinstance(prompt, bool):
            if self.first_prompt_tokens is None:
                self.first_prompt_tokens = prompt
            self.max_prompt_tokens = max(self.max_prompt_tokens, prompt)
        evals = result.get("eval_count")
        if isinstance(evals, int) and not isinstance(evals, bool):
            self.eval_tokens += evals
        reason = result.get("done_reason")
        if isinstance(reason, str):
            self.done_reason = reason
        num_ctx = result.get("num_ctx")
        if isinstance(num_ctx, int) and not isinstance(num_ctx, bool) and num_ctx > 0:
            self.num_ctx = num_ctx

    @property
    def truncated(self) -> bool:
        return self.done_reason == "length"


PartialSink = Callable[[str, bool], Awaitable[None]]


async def _poll_partial(
    node_link: ServiceLink,
    dst: Address,
    request_id: str,
    on_partial: PartialSink,
) -> None:
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
    reason: str,
    telegram_chat_id: int | None,
    log_chat_id: Any,
    on_tool_call: ToolCallSink | None = None,
    on_speech_remark: SpeechRemarkSink | None = None,
    role: str | None = None,
    on_partial: PartialSink | None = None,
    on_tool_start: ToolStartSink | None = None,
    photo_key: str | None = None,
    speech_user_id: int | None = None,
    speech_clear: bool | Callable[[], Awaitable[bool]] | None = None,
    stats: ChatStats | None = None,
    allow_tools: bool = True,
) -> str:
    tool_ctx.history = messages
    toolkit = ai_tools.for_context(ai_tools.tools_for(tool_ctx.subscription), tool_ctx)

    async def _chat_args(tools: list[dict[str, Any]]) -> dict[str, Any]:
        args: dict[str, Any] = {"messages": messages, "tools": tools, "reason": reason}
        if telegram_chat_id is not None:
            args["chat_id"] = telegram_chat_id
        if role is not None:
            args["role"] = role
        if photo_key is not None:
            args["photo_key"] = photo_key
        if speech_user_id is not None:
            args["user_id"] = speech_user_id
            clear = speech_clear() if callable(speech_clear) else speech_clear
            if isinstance(clear, Awaitable):
                clear = await clear
            if clear is not None:
                args["speech_clear"] = clear
        return args

    async def _maybe_send_remark(result: dict[str, Any]) -> None:
        remark = result.get("speech_remark")
        if remark and on_speech_remark is not None:
            await on_speech_remark(remark)

    async def _call_chat(args: dict[str, Any]) -> dict[str, Any]:
        if on_partial is None:
            return await node_link.command(ACTION_CHAT, args, dst=dst, timeout=timeout)
        request_id = new_id()
        streamed_args = {**args, "request_id": request_id}
        poll_task = asyncio.create_task(_poll_partial(node_link, dst, request_id, on_partial))
        try:
            return await node_link.command(ACTION_CHAT, streamed_args, dst=dst, timeout=timeout)
        finally:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

    async def _call_chat_with_retry(args: dict[str, Any]) -> dict[str, Any]:
        for delay in CHAT_RETRY_DELAYS_S:
            try:
                return await _call_chat(args)
            except (ServiceUnavailableError, ProtoError) as exc:
                if not _is_link_gap(exc):
                    raise
                log.warning(
                    "llm_chat: нет связи со службой llm (%s), повтор через %.0f с (chat=%s)",
                    exc,
                    delay,
                    log_chat_id,
                )
            await asyncio.sleep(delay)
        return await _call_chat(args)

    declarations = toolkit.declarations if allow_tools else []
    for _round in range(MAX_TOOL_ROUNDS):
        args = await _chat_args(declarations)
        result = await _call_chat_with_retry(args)
        if stats is not None:
            stats.absorb(result)
        tool_calls = result.get("tool_calls")
        if not tool_calls:
            await _maybe_send_remark(result)
            return result.get("response", "")
        messages.append({"role": "assistant", "tool_calls": tool_calls})
        serial_done = False
        for call in tool_calls:
            fn = call.get("function", {}) if isinstance(call, dict) else {}
            name = fn.get("name", "")
            call_args = fn.get("arguments") or {}
            handler = toolkit.handlers.get(name) if allow_tools else None
            if handler is None:
                tool_result = f"неизвестный инструмент: {name}"
            elif name in ai_tools.SERIAL_TOOLS and serial_done:
                tool_result = ai_tools.SERIAL_DEFERRED
            else:
                serial_done = serial_done or name in ai_tools.SERIAL_TOOLS
                if on_tool_start is not None:
                    await on_tool_start(name, call_args)
                try:
                    tool_result = await handler(tool_ctx, call_args)
                except Exception as exc:
                    log.exception("llm_chat: тул %s упал (chat=%s)", name, log_chat_id)
                    tool_result = f"внутренняя ошибка инструмента: {exc}"
            log.info(
                "llm_chat: тул %s(%s) -> %s (chat=%s)", name, call_args, tool_result, log_chat_id
            )
            if on_tool_call is not None:
                await on_tool_call(name, call_args, tool_result)
            inline_result = _inline_or_cache(tool_ctx, tool_result)
            messages.append({"role": "tool", "content": inline_result, "name": name})
        if tool_ctx.end_turn:
            return ""
    log.info(
        "llm_chat: лимит раундов (%d) исчерпан, дожимаем ответ без тулов (chat=%s)",
        MAX_TOOL_ROUNDS,
        log_chat_id,
    )
    result = await _call_chat_with_retry(await _chat_args([]))
    if stats is not None:
        stats.absorb(result)
    response = result.get("response", "")
    if response:
        await _maybe_send_remark(result)
        return response
    raise ProtoError(ERR_INTERNAL, "модель не дала ответа после лимита раундов")
