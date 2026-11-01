from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sa_home_bot.devlab.agent.context import estimate_tokens, fold_old_results
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.model import ChatModel, ModelError, PaidFreeModelError, ToolCall
from sa_home_bot.devlab.agent.tools import (
    TOOL_ASK_OWNER,
    TOOL_FINISH,
    TOOL_STEP_DONE,
    ToolBox,
    ToolError,
)

log = logging.getLogger(__name__)

NUDGE_TEXT = (
    "Ответ без вызова инструмента не принимается. Работай инструментами; "
    f"закончив, вызови {TOOL_FINISH}."
)
FINISH_REJECTED = "финал отклонён, продолжай работу:\n"
STEP_REJECTED = "шаг не принят, продолжай работу:\n"
STEP_ACCEPTED = "шаг принят"
ASK_SKIPPED = "не выполнено: задан вопрос владельцу"
CRANE_QUESTION = (
    "Стоп-кран: {reason}. Продолжить (тогда выдаётся новый отрезок лимитов) или остановить?"
)


class Crane(StrEnum):
    RED_RUNS = "red_runs"
    STEPS = "steps"
    WALL = "wall"
    COST = "cost"
    TOKENS = "tokens"
    PAID_FREE = "paid_free"


class AgentStatus(StrEnum):
    FINISHED = "finished"
    ASKED = "asked"
    MODEL_FAILED = "model_failed"
    CONTEXT_OVERFLOW = "context_overflow"
    STUCK = "stuck"


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 60
    max_wall_s: float = 8 * 3600.0
    max_red_runs: int = 5
    max_cost_usd: float = 2.0
    max_tokens: int = 0
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
    dialog: Dialog | None = None
    crane: Crane | None = None


ChangeListener = Callable[[str], Awaitable[None]]
StepListener = Callable[[str], Awaitable[None]]
SpendMeter = Callable[[], float]
TokenMeter = Callable[[], int]


@dataclass
class Segment:
    messages: list[dict[str, Any]]
    started: float
    steps: int = 0
    idle: int = 0
    red_runs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    asked: tuple[str, str | None] | None = None


class AgentLoop:
    def __init__(
        self,
        model: ChatModel,
        tools: ToolBox,
        guard: FinishGuard,
        limits: AgentLimits | None = None,
        on_change: ChangeListener | None = None,
        on_step: StepListener | None = None,
        spend: SpendMeter | None = None,
        tokens: TokenMeter | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._tools = tools
        self._guard = guard
        self._limits = limits or AgentLimits()
        self._on_change = on_change
        self._on_step = on_step
        self._spend = spend
        self._tokens = tokens
        self._clock = clock

    def _result(self, status: AgentStatus, seg: Segment, **fields: Any) -> AgentResult:
        return AgentResult(
            status,
            steps=seg.steps,
            tokens_in=seg.tokens_in,
            tokens_out=seg.tokens_out,
            **fields,
        )

    def _crane_result(
        self, seg: Segment, messages: list[dict[str, Any]], crane: Crane, reason: str
    ) -> AgentResult:
        question = CRANE_QUESTION.format(reason=reason)
        return self._result(
            AgentStatus.ASKED,
            seg,
            reason=reason,
            dialog=Dialog(messages=messages, question=question),
            crane=crane,
        )

    def _crane(self, seg: Segment) -> tuple[Crane, str] | None:
        limits = self._limits
        if seg.red_runs >= limits.max_red_runs:
            return Crane.RED_RUNS, f"{seg.red_runs} красных прогонов тестов подряд"
        if seg.steps >= limits.max_steps:
            return Crane.STEPS, f"исчерпан лимит шагов {limits.max_steps}"
        if self._clock() - seg.started >= limits.max_wall_s:
            return Crane.WALL, f"исчерпан бюджет времени {limits.max_wall_s:g} с"
        if self._spend is not None and self._spend() >= limits.max_cost_usd:
            return Crane.COST, f"исчерпан потолок расхода ${limits.max_cost_usd:g}"
        spent_tokens = self._tokens() if self._tokens else seg.tokens_in + seg.tokens_out
        if limits.max_tokens and spent_tokens >= limits.max_tokens:
            return Crane.TOKENS, f"исчерпан потолок токенов {limits.max_tokens}"
        return None

    async def run(self, system: str, task: str, resume: Dialog | None = None) -> AgentResult:
        limits = self._limits
        messages: list[dict[str, Any]] = (
            resume.resumed_messages()
            if resume is not None
            else [{"role": "system", "content": system}, {"role": "user", "content": task}]
        )
        seg = Segment(messages, self._clock())
        budget = (
            limits.context_tokens - limits.reply_tokens - estimate_tokens(self._tools.definitions)
        )
        while True:
            crane = self._crane(seg)
            if crane is not None:
                return self._crane_result(seg, messages, *crane)
            if fold_old_results(messages, budget, limits.keep_recent) > budget:
                return self._result(
                    AgentStatus.CONTEXT_OVERFLOW,
                    seg,
                    reason=f"окно {limits.context_tokens} ток. не вмещает даже свёрнутый диалог",
                )
            seg.steps += 1
            try:
                reply = await self._model.complete(messages, self._tools.definitions)
            except PaidFreeModelError as exc:
                return self._crane_result(seg, messages, Crane.PAID_FREE, str(exc))
            except ModelError as exc:
                return self._result(AgentStatus.MODEL_FAILED, seg, reason=str(exc))
            seg.tokens_in += reply.tokens_in
            seg.tokens_out += reply.tokens_out
            messages.append(reply.as_message())
            if not reply.tool_calls:
                seg.idle += 1
                if seg.idle > limits.max_idle_replies:
                    return self._result(
                        AgentStatus.STUCK,
                        seg,
                        reason=f"модель {seg.idle} раз ответила без инструментов",
                    )
                messages.append({"role": "user", "content": NUDGE_TEXT})
                continue
            seg.idle = 0
            finished = await self._handle_calls(reply.tool_calls, seg)
            if finished is not None:
                return self._result(AgentStatus.FINISHED, seg, summary=finished)
            if seg.asked is not None:
                question, call_id = seg.asked
                return self._result(
                    AgentStatus.ASKED,
                    seg,
                    reason=question,
                    dialog=Dialog(messages=messages, question=question, call_id=call_id),
                )

    async def _handle_calls(self, calls: tuple[ToolCall, ...], seg: Segment) -> str | None:
        for index, call in enumerate(calls):
            text, summary = await self._execute(call, seg)
            if seg.asked is not None:
                seg.asked = (seg.asked[0], call.id)
                for skipped in calls[index + 1 :]:
                    seg.messages.append(
                        {"role": "tool", "tool_call_id": skipped.id, "content": ASK_SKIPPED}
                    )
                return None
            seg.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
            if summary is not None:
                return summary
        return None

    async def _execute(self, call: ToolCall, seg: Segment) -> tuple[str, str | None]:
        try:
            args = json.loads(call.arguments)
        except ValueError:
            return "ошибка: аргументы не являются JSON", None
        if not isinstance(args, dict):
            return "ошибка: аргументы должны быть JSON-объектом", None
        if call.name == TOOL_FINISH:
            return await self._finish(args)
        if call.name == TOOL_STEP_DONE:
            return await self._step_done(args, seg), None
        if call.name == TOOL_ASK_OWNER:
            return self._ask(args, seg), None
        try:
            outcome = await self._tools.call(call.name, args)
        except ToolError as exc:
            return f"ошибка: {exc}", None
        except Exception as exc:
            log.warning("Инструмент %s упал: %s", call.name, type(exc).__name__)
            return f"ошибка инструмента: {type(exc).__name__}: {exc}", None
        if outcome.green is not None:
            seg.red_runs = 0 if outcome.green else seg.red_runs + 1
        if outcome.mutating and self._on_change is not None:
            await self._on_change(f"{call.name}: {outcome.text.splitlines()[0]}")
        return outcome.text, None

    def _ask(self, args: dict[str, Any], seg: Segment) -> str:
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            return "ошибка: ask_owner требует непустой question"
        seg.asked = (question.strip(), None)
        return ""

    async def _step_done(self, args: dict[str, Any], seg: Segment) -> str:
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            return "ошибка: step_done требует непустой title"
        failures = await self._guard.verify_green()
        if failures:
            seg.red_runs += 1
            return STEP_REJECTED + "\n".join(failures)
        seg.red_runs = 0
        if self._on_step is not None:
            await self._on_step(title.strip())
        return STEP_ACCEPTED

    async def _finish(self, args: dict[str, Any]) -> tuple[str, str | None]:
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return "ошибка: finish требует непустой summary", None
        failures = await self._guard.verify()
        if failures:
            return FINISH_REJECTED + "\n".join(failures), None
        return "принято", summary.strip()
