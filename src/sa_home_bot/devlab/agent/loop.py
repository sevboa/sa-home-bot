from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sa_home_bot.devlab.agent.context import estimate_tokens, fold_old_results
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.model import ChatModel, ModelError, ToolCall
from sa_home_bot.devlab.agent.tools import TOOL_FINISH, ToolBox, ToolError

log = logging.getLogger(__name__)

NUDGE_TEXT = (
    "Ответ без вызова инструмента не принимается. Работай инструментами; "
    f"закончив, вызови {TOOL_FINISH}."
)
FINISH_REJECTED = "финал отклонён, продолжай работу:\n"


class AgentStatus(StrEnum):
    FINISHED = "finished"
    STEP_LIMIT = "step_limit"
    MODEL_FAILED = "model_failed"
    CONTEXT_OVERFLOW = "context_overflow"
    STUCK = "stuck"


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 60
    max_idle_replies: int = 3
    context_tokens: int = 16000
    reply_tokens: int = 2000
    keep_recent: int = 4


@dataclass(frozen=True)
class AgentResult:
    status: AgentStatus
    summary: str = ""
    reason: str = ""
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


ChangeListener = Callable[[str], Awaitable[None]]


class AgentLoop:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolBox,
        guard: FinishGuard,
        limits: AgentLimits | None = None,
        on_change: ChangeListener | None = None,
    ) -> None:
        self._model = model
        self._tools = tools
        self._guard = guard
        self._limits = limits or AgentLimits()
        self._on_change = on_change

    async def run(self, system: str, task: str) -> AgentResult:
        limits = self._limits
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        budget = (
            limits.context_tokens - limits.reply_tokens - estimate_tokens(self._tools.definitions)
        )
        tokens_in = tokens_out = idle = steps = 0
        while steps < limits.max_steps:
            if fold_old_results(messages, budget, limits.keep_recent) > budget:
                return AgentResult(
                    AgentStatus.CONTEXT_OVERFLOW,
                    reason=f"окно {limits.context_tokens} ток. не вмещает даже свёрнутый диалог",
                    steps=steps,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            steps += 1
            try:
                reply = await self._model.complete(messages, self._tools.definitions)
            except ModelError as exc:
                return AgentResult(
                    AgentStatus.MODEL_FAILED,
                    reason=str(exc),
                    steps=steps,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            tokens_in += reply.tokens_in
            tokens_out += reply.tokens_out
            messages.append(reply.as_message())
            if not reply.tool_calls:
                idle += 1
                if idle > limits.max_idle_replies:
                    return AgentResult(
                        AgentStatus.STUCK,
                        reason=f"модель {idle} раз ответила без инструментов",
                        steps=steps,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                    )
                messages.append({"role": "user", "content": NUDGE_TEXT})
                continue
            idle = 0
            finished = await self._handle_calls(reply.tool_calls, messages)
            if finished is not None:
                return AgentResult(
                    AgentStatus.FINISHED,
                    summary=finished,
                    steps=steps,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
        return AgentResult(
            AgentStatus.STEP_LIMIT,
            reason=f"исчерпан лимит шагов {limits.max_steps}",
            steps=steps,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def _handle_calls(
        self, calls: tuple[ToolCall, ...], messages: list[dict[str, Any]]
    ) -> str | None:
        for call in calls:
            text, summary = await self._execute(call)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
            if summary is not None:
                return summary
        return None

    async def _execute(self, call: ToolCall) -> tuple[str, str | None]:
        try:
            args = json.loads(call.arguments)
        except ValueError:
            return "ошибка: аргументы не являются JSON", None
        if not isinstance(args, dict):
            return "ошибка: аргументы должны быть JSON-объектом", None
        if call.name == TOOL_FINISH:
            return await self._finish(args)
        try:
            outcome = await self._tools.call(call.name, args)
        except ToolError as exc:
            return f"ошибка: {exc}", None
        except Exception as exc:
            log.warning("Инструмент %s упал: %s", call.name, type(exc).__name__)
            return f"ошибка инструмента: {type(exc).__name__}: {exc}", None
        if outcome.mutating and self._on_change is not None:
            await self._on_change(f"{call.name}: {outcome.text.splitlines()[0]}")
        return outcome.text, None

    async def _finish(self, args: dict[str, Any]) -> tuple[str, str | None]:
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return "ошибка: finish требует непустой summary", None
        failures = await self._guard.verify()
        if failures:
            return FINISH_REJECTED + "\n".join(failures), None
        return "принято", summary.strip()
