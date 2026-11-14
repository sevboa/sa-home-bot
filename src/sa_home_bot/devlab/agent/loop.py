from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sa_home_bot.devlab.agent.context import (
    estimate_tokens,
    fold_old_results,
    pinned_report,
    shrink_arguments,
)
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.model import (
    ChatModel,
    ModelError,
    ModelReply,
    PaidFreeModelError,
    ToolCall,
)
from sa_home_bot.devlab.agent.sandbox import OK_HEAD
from sa_home_bot.devlab.agent.tools import (
    READ_ONLY_TOOLS,
    TOOL_ASK_OWNER,
    TOOL_FINISH,
    TOOL_STEP_DONE,
    ToolBox,
    ToolError,
)
from sa_home_bot.devlab.agent.trace import TraceSink

log = logging.getLogger(__name__)

NUDGE_TEXT = (
    "Ответ без вызова инструмента не принимается. Работай инструментами; "
    f"закончив, вызови {TOOL_FINISH}."
)
FINISH_REJECTED = (
    f"финал отклонён: исправь и вызови {TOOL_FINISH} снова; полный вывод стража ниже:\n"
)
STEP_REJECTED = (
    f"шаг не принят: исправь и вызови {TOOL_STEP_DONE} снова; полный вывод стража ниже:\n"
)
STEP_ACCEPTED = "шаг принят"
TRACE_TEXT_CHARS = 300
ASK_SKIPPED = "не выполнено: задан вопрос владельцу"
CRANE_QUESTION = (
    "Стоп-кран: {reason}. Продолжить (тогда выдаётся новый отрезок лимитов) или остановить?"
)
REPEAT_HINT = "\n\nэтот вызов уже был, результат тот же, попробуй иначе"
REPEAT_GREEN_HINT = (
    "\n\nэтот вызов уже был, результат тот же и зелёный: не повторяй проверку; "
    f"если все шаги сделаны, вызови {TOOL_FINISH}"
)
REPEAT_REASON = "модель повторяет {call}"
REPEAT_SKIPPED = "не выполнено: стоп-кран на повторах"
RECENT_CALLS_WINDOW = 6
OVERFLOW_REASON = (
    "{pinned}, схемы инструментов {tools} ток.; бюджет диалога {budget} ток. "
    "при окне {window} ток.: "
    "сократи [agent] rules или task.md"
)


class Crane(StrEnum):
    RED_RUNS = "red_runs"
    STEPS = "steps"
    WALL = "wall"
    COST = "cost"
    TOKENS = "tokens"
    PAID_FREE = "paid_free"
    REPEAT = "repeat"


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
    max_repeats: int = 3
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
    calls: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    recent_calls: list[tuple[str, str]] = field(default_factory=list)
    repeat_reason: str | None = None
    tree_fingerprint: str | None = None


def _call_label(name: str, args: dict[str, Any]) -> str:
    target = args.get("path") or args.get("command") or ""
    return f"{name} {target}".strip()


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
        trace: TraceSink | None = None,
        clock: Callable[[], float] = time.monotonic,
        overflow_hint: str = "",
    ) -> None:
        self._model = model
        self._tools = tools
        self._guard = guard
        self._limits = limits or AgentLimits()
        self._on_change = on_change
        self._on_step = on_step
        self._spend = spend
        self._tokens = tokens
        self._trace = trace
        self._clock = clock
        self._overflow_hint = overflow_hint

    def _emit(self, **entry: Any) -> None:
        if self._trace is not None:
            self._trace({"t": int(time.time()), **entry})

    def _emit_step(self, seg: Segment, reply: ModelReply, window: int, folded: bool) -> None:
        self._emit(
            step=seg.steps,
            tokens_in=reply.tokens_in,
            tokens_out=reply.tokens_out,
            cost_usd=reply.cost_usd,
            window=window,
            folded=folded,
            text=reply.content[:TRACE_TEXT_CHARS] if not reply.tool_calls else "",
            calls=seg.calls,
            skipped=seg.skipped,
        )

    def _result(self, status: AgentStatus, seg: Segment, **fields: Any) -> AgentResult:
        crane = fields.get("crane")
        self._emit(
            event="end",
            status=status.value,
            reason=str(fields.get("reason", ""))[:TRACE_TEXT_CHARS],
            crane=crane.value if crane else None,
            steps=seg.steps,
        )
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
        self._emit(event="start", resumed=resume is not None)
        tools_tokens = estimate_tokens(self._tools.definitions)
        budget = limits.context_tokens - limits.reply_tokens - tools_tokens
        while True:
            crane = self._crane(seg)
            if crane is not None:
                return self._crane_result(seg, messages, *crane)
            before = estimate_tokens(messages) if self._trace is not None else 0
            window = fold_old_results(messages, budget, limits.keep_recent)
            if window > budget:
                return self._result(
                    AgentStatus.CONTEXT_OVERFLOW,
                    seg,
                    reason=OVERFLOW_REASON.format(
                        pinned=pinned_report(messages),
                        tools=tools_tokens,
                        budget=budget,
                        window=limits.context_tokens,
                    )
                    + self._overflow_hint,
                )
            seg.steps += 1
            seg.calls = []
            seg.skipped = 0
            seg.repeat_reason = None
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
                self._emit_step(seg, reply, window, window < before)
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
            self._emit_step(seg, reply, window, window < before)
            if finished is not None:
                return self._result(AgentStatus.FINISHED, seg, summary=finished)
            if seg.repeat_reason is not None:
                return self._crane_result(seg, messages, Crane.REPEAT, seg.repeat_reason)
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
            self._note_call(call, text, seg)
            if seg.repeat_reason is not None:
                seg.skipped = len(calls) - index - 1
                seg.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
                for skipped in calls[index + 1 :]:
                    seg.messages.append(
                        {"role": "tool", "tool_call_id": skipped.id, "content": REPEAT_SKIPPED}
                    )
                return None
            if seg.asked is not None:
                seg.asked = (seg.asked[0], call.id)
                seg.skipped = len(calls) - index - 1
                for skipped in calls[index + 1 :]:
                    seg.messages.append(
                        {"role": "tool", "tool_call_id": skipped.id, "content": ASK_SKIPPED}
                    )
                return None
            seg.messages.append({"role": "tool", "tool_call_id": call.id, "content": text})
            if summary is not None:
                return summary
        return None

    def _note_call(self, call: ToolCall, text: str, seg: Segment) -> None:
        if self._trace is None:
            return
        try:
            args = json.loads(shrink_arguments(call.arguments))
        except ValueError:
            args = {}
        seg.calls.append(
            {
                "name": call.name,
                "args": args,
                "result": text[:TRACE_TEXT_CHARS],
                "result_chars": len(text),
                "rejected": text.startswith((FINISH_REJECTED, STEP_REJECTED)),
            }
        )

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
        repeats = await self._count_repeats(call, args, seg)
        if repeats + 1 >= self._limits.max_repeats:
            seg.repeat_reason = REPEAT_REASON.format(call=_call_label(call.name, args))
            return REPEAT_SKIPPED, None
        text, summary = await self._run_tool(call, args, seg)
        if call.name not in READ_ONLY_TOOLS:
            seg.tree_fingerprint = None
        if repeats:
            text += REPEAT_GREEN_HINT if text.startswith(OK_HEAD) else REPEAT_HINT
        return text, summary

    async def _count_repeats(self, call: ToolCall, args: dict[str, Any], seg: Segment) -> int:
        key = f"{call.name} {json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if seg.tree_fingerprint is None:
            seg.tree_fingerprint = await self._tools.tree_fingerprint()
        entry = (key, seg.tree_fingerprint)
        repeats = seg.recent_calls.count(entry)
        seg.recent_calls = [*seg.recent_calls, entry][-RECENT_CALLS_WINDOW:]
        return repeats

    async def _run_tool(
        self, call: ToolCall, args: dict[str, Any], seg: Segment
    ) -> tuple[str, str | None]:
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
