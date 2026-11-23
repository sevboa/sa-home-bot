from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sa_home_bot.devlab.agent.context import (
    estimate_tokens,
    fold_old_results,
    pinned_report,
    set_notes_block,
    shrink_arguments,
    task_text,
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
from sa_home_bot.devlab.agent.script import SCRIPT_FAILED_HEAD
from sa_home_bot.devlab.agent.tools import (
    READ_ONLY_TOOLS,
    TOOL_ASK_OWNER,
    TOOL_EDIT_FILE,
    TOOL_EXECUTE,
    TOOL_FINISH,
    TOOL_STEP_DONE,
    TOOL_WRITE_FILE,
    ControlRequest,
    ToolBox,
    ToolError,
)
from sa_home_bot.devlab.agent.trace import TraceSink

log = logging.getLogger(__name__)

NUDGE_TEXT = (
    "Ответ без вызова инструмента не принимается. Работай инструментами; "
    f"закончив, вызови {TOOL_FINISH}."
)
FINISH_REJECTED_HEAD = "финал отклонён"
FINISH_REJECTED = (
    f"{FINISH_REJECTED_HEAD}: исправь и вызови {TOOL_FINISH} снова; полный вывод стража ниже:\n"
)
STEP_REJECTED_HEAD = "шаг не принят"
STEP_REJECTED = (
    f"{STEP_REJECTED_HEAD}: исправь и вызови {TOOL_STEP_DONE} снова; шаг, который по заданию "
    "заканчивается красным тестом, закрывается вместе с правкой; полный вывод стража ниже:\n"
)
CODE_NUDGE_TEXT = (
    "Ответ без вызова инструмента не принимается. Пиши код в execute; "
    'закончив, вызови в нём finish("итог").'
)
CODE_FINISH_REJECTED = (
    f"{FINISH_REJECTED_HEAD}: исправь и снова вызови finish() в execute; "
    "полный вывод стража ниже:\n"
)
CODE_STEP_REJECTED = (
    f"{STEP_REJECTED_HEAD}: скрипт остановлен (StepRejected); исправь и снова вызови "
    "step_done() в следующем execute либо сразу вызови finish() после правок. "
    "step_done() необязателен: finish() закроет все шаги сам; "
    "полный вывод стража ниже:\n"
)
STEP_ACCEPTED = "шаг принят"
CODE_COUNTER = "[вызов {number} · контекст {thousands} тыс. токенов]"
SCRIPT_REJECTED = re.compile(
    rf"^(?:{TOOL_FINISH}: |StepRejected: )"
    rf"(?:{STEP_REJECTED_HEAD}|{FINISH_REJECTED_HEAD})",
    re.MULTILINE,
)
TRACE_TEXT_CHARS = 300
STEPS_HEADING = re.compile(r"^#{1,6}\s*(?:шаги|steps)\b", re.IGNORECASE)
ANY_HEADING = re.compile(r"^#{1,6}\s")
STEP_LINE = re.compile(r"^\s*(\d+)[.)]\s+\S")
STEP_TITLE_NUMBERS = re.compile(r"^\s*(\d+(?:\s*(?:[–-]|,)\s*\d+)*)")
FINISH_STEP_TITLE = "{number}: закрыт на finish: {summary}"
FINISH_STEP_SUMMARY_CHARS = 200
ASK_SKIPPED = "не выполнено: задан вопрос владельцу"
CRANE_QUESTION = (
    "Стоп-кран: {reason}. Продолжить (тогда выдаётся новый отрезок лимитов) или остановить?"
)
NUMBERS = re.compile(r"\d+")
SPACES = re.compile(r"\s+")
NUMBER_PLACEHOLDER = "#"
REPEAT_TURN = (
    "Этот вызов ты уже делал (вызов {number}), результат был выше и не изменился. "
    "Прочитанное держи в переменных. Следующий вызов должен изменить файл "
    f"({TOOL_EDIT_FILE}/{TOOL_WRITE_FILE}), запустить проверку или вызвать {TOOL_FINISH}()/"
    f"{TOOL_ASK_OWNER}(). Если не знаешь, что править, — {TOOL_ASK_OWNER}()."
)
CODE_REPEAT_TURN = (
    "Этот вызов ты уже делал (вызов {number}; повтором считается и тот же вызов с другими "
    "числами), результат был выше и не изменился. "
    "Прочитанное держи в переменных. Следующий вызов execute должен изменить файл "
    "(edit/write), запустить проверку или вызвать finish()/ask_owner(). "
    "Если не знаешь, что править, — ask_owner()."
)
REPEAT_REASON = "модель повторяет {call}"
REPEAT_SKIPPED = "не выполнено: стоп-кран на повторах"
RECENT_CALLS_WINDOW = 6
REPEAT_TURNS = 2
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
    max_near_repeats: int = 6
    context_tokens: int = 16000
    reply_tokens: int = 2000
    keep_recent: int = 4
    quick_steps: int = 0
    quick_params: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)


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
    first_numbers: dict[tuple[str, str], int] = field(default_factory=dict)
    repeated: int = 0
    near_entry: tuple[str, str] | None = None
    near_count: int = 0
    near_number: int = 0
    near_hit: bool = False
    turn_number: int = 0
    turn_message: dict[str, Any] | None = None
    repeat_reason: str | None = None
    tree_fingerprint: str | None = None
    context_tokens: int = 0


def _rejected(name: str, text: str) -> bool:
    if name == TOOL_EXECUTE:
        return SCRIPT_REJECTED.search(text) is not None
    return text.startswith(
        (FINISH_REJECTED, STEP_REJECTED, CODE_FINISH_REJECTED, CODE_STEP_REJECTED)
    )


def _task_steps(task: str) -> list[int]:
    numbers: list[int] = []
    inside = False
    for line in task.splitlines():
        if ANY_HEADING.match(line):
            inside = STEPS_HEADING.match(line) is not None
            continue
        found = STEP_LINE.match(line) if inside else None
        if found and int(found.group(1)) not in numbers:
            numbers.append(int(found.group(1)))
    return numbers


def _title_numbers(title: str) -> set[int]:
    found = STEP_TITLE_NUMBERS.match(title)
    if not found:
        return set()
    text = found.group(1)
    numbers: set[int] = set()
    for part in text.split(","):
        bounds = [int(n) for n in re.findall(r"\d+", part)]
        if len(bounds) == 2 and bounds[0] <= bounds[1]:
            numbers.update(range(bounds[0], bounds[1] + 1))
        else:
            numbers.update(bounds)
    return numbers


def _script_code(arguments: str) -> str:
    try:
        code = json.loads(arguments).get("code", "")
    except (ValueError, AttributeError):
        return ""
    return code if isinstance(code, str) else ""


def _near_key(name: str, args: dict[str, Any]) -> str | None:
    code = args.get("code")
    if name != TOOL_EXECUTE or not isinstance(code, str):
        return None
    return SPACES.sub(" ", NUMBERS.sub(NUMBER_PLACEHOLDER, code)).strip()


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
        self._script_calls = 0
        self._task_steps: list[int] = []
        self._closed_steps: set[int] = set()

    def _emit(self, **entry: Any) -> None:
        if self._trace is not None:
            self._trace({"t": int(time.time()), **entry})

    def _emit_step(
        self,
        seg: Segment,
        reply: ModelReply,
        window: int,
        folded: bool,
        model_s: float,
        quick: bool = False,
    ) -> None:
        self._emit(
            **({"quick": True} if quick else {}),
            step=seg.steps,
            model_s=model_s,
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
        set_notes_block(messages, self._tools.notes_text())
        self._task_steps = _task_steps(task_text(messages))
        self._emit(event="start", resumed=resume is not None)
        tools_tokens = estimate_tokens(self._tools.definitions)
        budget = limits.context_tokens - limits.reply_tokens - tools_tokens
        while True:
            crane = self._crane(seg)
            if crane is not None:
                return self._crane_result(seg, messages, *crane)
            before = estimate_tokens(messages) if self._trace is not None else 0
            window = fold_old_results(
                messages, budget, limits.keep_recent, self._tools.notes_text
            )
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
            seg.near_hit = False
            seg.repeated = 0
            asked_at = self._clock()
            quick = bool(limits.quick_params) and seg.steps <= limits.quick_steps
            extra = {**limits.params, **(limits.quick_params if quick else {})}
            try:
                reply = await (
                    self._model.complete(messages, self._tools.definitions, extra=extra)
                    if extra
                    else self._model.complete(messages, self._tools.definitions)
                )
            except PaidFreeModelError as exc:
                return self._crane_result(seg, messages, Crane.PAID_FREE, str(exc))
            except ModelError as exc:
                return self._result(AgentStatus.MODEL_FAILED, seg, reason=str(exc))
            model_s = round(self._clock() - asked_at, 2)
            seg.tokens_in += reply.tokens_in
            seg.context_tokens = reply.tokens_in
            seg.tokens_out += reply.tokens_out
            messages.append(reply.as_message())
            if not reply.tool_calls:
                self._emit_step(seg, reply, window, window < before, model_s, quick)
                seg.idle += 1
                if seg.idle > limits.max_idle_replies:
                    return self._result(
                        AgentStatus.STUCK,
                        seg,
                        reason=f"модель {seg.idle} раз ответила без инструментов",
                    )
                nudge = CODE_NUDGE_TEXT if self._tools.code_mode else NUDGE_TEXT
                messages.append({"role": "user", "content": nudge})
                continue
            seg.idle = 0
            reply_index = len(messages) - 1
            finished = await self._handle_calls(reply.tool_calls, seg)
            self._emit_step(seg, reply, window, window < before, model_s, quick)
            if (
                finished is None
                and seg.repeat_reason is None
                and seg.asked is None
                and seg.repeated == len(reply.tool_calls)
            ):
                self._collapse_repeat(messages, reply_index, seg)
                continue
            if finished is not None:
                return self._result(AgentStatus.FINISHED, seg, summary=finished)
            if seg.repeat_reason is not None:
                if seg.near_hit:
                    template = CODE_REPEAT_TURN if self._tools.code_mode else REPEAT_TURN
                    messages.append(
                        {"role": "user", "content": template.format(number=seg.near_number)}
                    )
                return self._crane_result(seg, messages, Crane.REPEAT, seg.repeat_reason)
            if seg.asked is not None:
                question, call_id = seg.asked
                return self._result(
                    AgentStatus.ASKED,
                    seg,
                    reason=question,
                    dialog=Dialog(messages=messages, question=question, call_id=call_id),
                )

    def _collapse_repeat(self, messages: list[dict[str, Any]], index: int, seg: Segment) -> None:
        del messages[index:]
        if seg.turn_message is not None and messages and messages[-1] is seg.turn_message:
            messages.pop()
        template = CODE_REPEAT_TURN if self._tools.code_mode else REPEAT_TURN
        seg.turn_message = {"role": "user", "content": template.format(number=seg.turn_number)}
        messages.append(seg.turn_message)

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
        entry = {
            "name": call.name,
            "args": args,
            "result": text[:TRACE_TEXT_CHARS],
            "result_chars": len(text),
            "rejected": _rejected(call.name, text),
        }
        if call.name == TOOL_EXECUTE:
            entry["code_chars"] = len(_script_code(call.arguments))
            entry["script_failed"] = text.startswith(SCRIPT_FAILED_HEAD)
        seg.calls.append(entry)

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
        repeats, entry = await self._count_repeats(call, args, seg)
        if self._count_near(call, args, seg):
            seg.near_hit = True
            seg.repeat_reason = REPEAT_REASON.format(call=_call_label(call.name, args))
            return REPEAT_SKIPPED, None
        if repeats and entry in seg.first_numbers:
            if repeats + 1 >= self._limits.max_repeats + REPEAT_TURNS:
                seg.repeat_reason = REPEAT_REASON.format(call=_call_label(call.name, args))
                return REPEAT_SKIPPED, None
            seg.repeated += 1
            seg.turn_number = seg.first_numbers[entry]
            template = CODE_REPEAT_TURN if self._tools.code_mode else REPEAT_TURN
            return template.format(number=seg.turn_number), None
        text, summary = await self._run_tool(call, args, seg)
        if call.name not in READ_ONLY_TOOLS:
            seg.tree_fingerprint = None
        number = seg.steps
        if call.name == TOOL_EXECUTE:
            self._script_calls += 1
            number = self._script_calls
            counter = CODE_COUNTER.format(number=number, thousands=round(seg.context_tokens / 1000))
            text = f"{text}\n{counter}" if text else counter
        seg.first_numbers[entry] = number
        if seg.near_count == 1:
            seg.near_number = number
        return text, summary

    async def _count_repeats(
        self, call: ToolCall, args: dict[str, Any], seg: Segment
    ) -> tuple[int, tuple[str, str]]:
        key = f"{call.name} {json.dumps(args, sort_keys=True, ensure_ascii=False)}"
        if seg.tree_fingerprint is None:
            seg.tree_fingerprint = await self._tools.tree_fingerprint()
        entry = (key, seg.tree_fingerprint)
        repeats = seg.recent_calls.count(entry)
        seg.recent_calls = [*seg.recent_calls, entry][-RECENT_CALLS_WINDOW:]
        return repeats, entry

    def _count_near(self, call: ToolCall, args: dict[str, Any], seg: Segment) -> bool:
        near = _near_key(call.name, args)
        if near is None or seg.tree_fingerprint is None:
            seg.near_entry, seg.near_count = None, 0
            return False
        entry = (near, seg.tree_fingerprint)
        seg.near_count = seg.near_count + 1 if entry == seg.near_entry else 1
        seg.near_entry = entry
        return seg.near_count >= self._limits.max_near_repeats

    async def _run_tool(
        self, call: ToolCall, args: dict[str, Any], seg: Segment
    ) -> tuple[str, str | None]:
        try:
            outcome = await self._tools.call(
                call.name, args, step_gate=lambda step_args: self._check_step(step_args, seg)
            )
        except ToolError as exc:
            return f"ошибка: {exc}", None
        except Exception as exc:
            log.warning("Инструмент %s упал: %s", call.name, type(exc).__name__)
            return f"ошибка инструмента: {type(exc).__name__}: {exc}", None
        if outcome.green is not None:
            seg.red_runs = 0 if outcome.green else seg.red_runs + 1
        if outcome.mutating and self._on_change is not None:
            await self._on_change(f"{call.name}: {(outcome.text.splitlines() or [''])[0]}")
        if outcome.control is None:
            return outcome.text, None
        return await self._close_from_script(outcome.text, outcome.control, seg)

    async def _close_from_script(
        self, text: str, control: ControlRequest, seg: Segment
    ) -> tuple[str, str | None]:
        summary = None
        if control.tool == TOOL_FINISH:
            result, summary = await self._finish(control.args)
        else:
            result = self._ask(control.args, seg)
        parts = [text, f"{control.label}: {result}" if result else ""]
        return "\n".join(part for part in parts if part), summary

    def _ask(self, args: dict[str, Any], seg: Segment) -> str:
        question = args.get("question")
        if not isinstance(question, str) or not question.strip():
            return "ошибка: ask_owner требует непустой question"
        seg.asked = (question.strip(), None)
        return ""

    async def _step_done(self, args: dict[str, Any], seg: Segment) -> str:
        return await self._check_step(args, seg) or STEP_ACCEPTED

    async def _check_step(self, args: dict[str, Any], seg: Segment) -> str:
        title = args.get("title")
        if not isinstance(title, str) or not title.strip():
            return "ошибка: step_done требует непустой title"
        failures = await self._guard.verify_step()
        if failures:
            seg.red_runs += 1
            rejected = CODE_STEP_REJECTED if self._tools.code_mode else STEP_REJECTED
            return rejected + "\n".join(failures)
        seg.red_runs = 0
        self._closed_steps |= _title_numbers(title)
        if self._on_step is not None:
            await self._on_step(title.strip())
        return ""

    async def _finish(self, args: dict[str, Any]) -> tuple[str, str | None]:
        summary = args.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return "ошибка: finish требует непустой summary", None
        failures = self._guard.verify_summary(summary) or await self._guard.verify()
        if failures:
            rejected = CODE_FINISH_REJECTED if self._tools.code_mode else FINISH_REJECTED
            return rejected + "\n".join(failures), None
        await self._close_open_steps(summary.strip())
        return "принято", summary.strip()

    async def _close_open_steps(self, summary: str) -> None:
        if not self._tools.code_mode or self._on_step is None:
            return
        head = (summary.splitlines() or [""])[0][:FINISH_STEP_SUMMARY_CHARS]
        for number in self._task_steps:
            if number in self._closed_steps:
                continue
            self._closed_steps.add(number)
            await self._on_step(FINISH_STEP_TITLE.format(number=number, summary=head))
