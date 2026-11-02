from __future__ import annotations

import json
import re
from typing import Any

from sa_home_bot.devlab.agent.dialog import ANSWER_PREFIX

CHARS_PER_TOKEN = 3
FOLDED_OUTPUT_PREFIX = "[вывод свёрнут"
FOLDED_OUTPUT = FOLDED_OUTPUT_PREFIX + ": {size} симв.]"
ARGUMENT_KEEP_CHARS = 200
ARGUMENT_LONG_CHARS = 300
CUT_ARGUMENT = "{head}… [обрезано {size} симв.]"
FOLD_ARGUMENTS_MIN_CHARS = 400
PINNED_MESSAGES = 2
RECENT_OUTPUT_CHARS = 2000
FOLDED_REPLY_PREFIX = "[ответ свёрнут"
FOLDED_REPLY = FOLDED_REPLY_PREFIX + ": {size} симв.]"
TRUNCATED_MARK = "[…обрезано {size} симв.…]"
TRUNCATED_PATTERN = re.compile(r"\n\[…обрезано \d+ симв\.…\]\n")
DROPPED_STEPS = "[свёрнуто {count} шагов]"
DROPPED_STEPS_PATTERN = re.compile(r"\n\n\[свёрнуто (\d+) шагов\]$")
MARKER_RESERVE_CHARS = 60
SEPARATOR_CHARS = 2


def estimate_tokens(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False)) // CHARS_PER_TOKEN + 1


def _size(message: dict[str, Any]) -> int:
    return len(json.dumps(message, ensure_ascii=False))


class _Window:
    def __init__(self, messages: list[dict[str, Any]], budget_tokens: int) -> None:
        self.messages = messages
        self.limit = budget_tokens
        self.reset()

    def reset(self) -> None:
        messages = self.messages
        self.sizes = [_size(m) for m in messages]
        self.total = sum(self.sizes)

    def fits(self, extra: int = 0) -> bool:
        count = len(self.messages)
        chars = self.total + extra + SEPARATOR_CHARS * max(count - 1, 0) + 2
        return chars // CHARS_PER_TOKEN + 1 <= self.limit

    def touch(self, index: int) -> None:
        size = _size(self.messages[index])
        self.total += size - self.sizes[index]
        self.sizes[index] = size

    def tokens(self) -> int:
        return estimate_tokens(self.messages)


def _old_boundary(messages: list[dict[str, Any]], keep_recent: int) -> int:
    tool_indexes = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    if len(tool_indexes) <= keep_recent:
        return PINNED_MESSAGES
    return tool_indexes[-keep_recent] if keep_recent else len(messages)


def _fold_tool_outputs(window: _Window, boundary: int) -> bool:
    for index in range(PINNED_MESSAGES, boundary):
        message = window.messages[index]
        if message["role"] == "tool" and not message["content"].startswith(FOLDED_OUTPUT_PREFIX):
            message["content"] = FOLDED_OUTPUT.format(size=len(message["content"]))
            window.touch(index)
            if window.fits():
                return True
    return False


def _cut_value(value: str) -> str:
    return CUT_ARGUMENT.format(head=value[:ARGUMENT_KEEP_CHARS], size=len(value))


def shrink_arguments(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return json.dumps(
            {"обрезано": arguments[:ARGUMENT_KEEP_CHARS] + "…", "симв.": len(arguments)},
            ensure_ascii=False,
        )
    shrunk = {
        key: _cut_value(value)
        if isinstance(value, str) and len(value) > ARGUMENT_LONG_CHARS
        else value
        for key, value in parsed.items()
    }
    return json.dumps(shrunk, ensure_ascii=False)


def _fold_arguments(window: _Window, boundary: int) -> bool:
    for index in range(PINNED_MESSAGES, boundary):
        touched = False
        for call in window.messages[index].get("tool_calls") or ():
            function = call["function"]
            if len(function["arguments"]) >= FOLD_ARGUMENTS_MIN_CHARS:
                function["arguments"] = shrink_arguments(function["arguments"])
                touched = True
        if touched:
            window.touch(index)
            if window.fits():
                return True
    return False


def _fold_replies(window: _Window, boundary: int) -> bool:
    for index in range(PINNED_MESSAGES, boundary):
        message = window.messages[index]
        content = message.get("content")
        if (
            message["role"] == "assistant"
            and isinstance(content, str)
            and len(content) >= FOLD_ARGUMENTS_MIN_CHARS
            and not content.startswith(FOLDED_REPLY_PREFIX)
        ):
            message["content"] = FOLDED_REPLY.format(size=len(content))
            window.touch(index)
            if window.fits():
                return True
    return False


def _truncate(content: str) -> str:
    half = RECENT_OUTPUT_CHARS // 2
    cut = len(content) - 2 * half
    return content[:half] + "\n" + TRUNCATED_MARK.format(size=cut) + "\n" + content[-half:]


def _truncatable(content: Any) -> bool:
    return (
        isinstance(content, str)
        and len(content) > RECENT_OUTPUT_CHARS
        and TRUNCATED_PATTERN.search(content) is None
    )


def _is_answer(message: dict[str, Any]) -> bool:
    content = message.get("content")
    return (
        message["role"] == "user" and isinstance(content, str) and content.startswith(ANSWER_PREFIX)
    )


def _truncate_messages(window: _Window, roles: tuple[str, ...]) -> bool:
    for index in range(PINNED_MESSAGES, len(window.messages)):
        message = window.messages[index]
        if message["role"] not in roles or (message["role"] == "user" and not _is_answer(message)):
            continue
        if _truncatable(message["content"]):
            message["content"] = _truncate(message["content"])
            window.touch(index)
            if window.fits():
                return True
    return False


def _units(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    units = []
    start = PINNED_MESSAGES
    while start < len(messages):
        end = start + 1
        if messages[start]["role"] == "assistant" and messages[start].get("tool_calls"):
            while end < len(messages) and messages[end]["role"] == "tool":
                end += 1
        units.append((start, end))
        start = end
    return units


def _mark_task(messages: list[dict[str, Any]], dropped: int) -> None:
    task = messages[1]["content"]
    found = DROPPED_STEPS_PATTERN.search(task)
    previous = 0
    if found:
        previous = int(found.group(1))
        task = task[: found.start()]
    messages[1]["content"] = task + "\n\n" + DROPPED_STEPS.format(count=previous + dropped)


def _drop_batch(window: _Window) -> bool:
    units = _units(window.messages)[:-1]
    chosen: list[tuple[int, int]] = []
    freed = 0
    for start, end in units:
        if end - start == 1 and _is_answer(window.messages[start]):
            continue
        chosen.append((start, end))
        freed += sum(window.sizes[start:end]) + SEPARATOR_CHARS * (end - start)
        if window.fits(MARKER_RESERVE_CHARS - freed):
            break
    if not chosen:
        return False
    doomed = {i for start, end in chosen for i in range(start, end)}
    window.messages[:] = [m for i, m in enumerate(window.messages) if i not in doomed]
    _mark_task(window.messages, len(chosen))
    window.reset()
    return True


def _trim_last_unit(window: _Window) -> bool:
    units = _units(window.messages)
    if not units:
        return False
    start, end = units[-1]
    for index in range(start, end):
        message = window.messages[index]
        for call in message.get("tool_calls") or ():
            function = call["function"]
            if len(function["arguments"]) > RECENT_OUTPUT_CHARS:
                function["arguments"] = shrink_arguments(function["arguments"])
                window.touch(index)
                if window.fits():
                    return True
        if message["role"] != "tool" and _truncatable(message.get("content")):
            message["content"] = _truncate(message["content"])
            window.touch(index)
            if window.fits():
                return True
    return False


def fold_old_results(messages: list[dict[str, Any]], budget_tokens: int, keep_recent: int) -> int:
    window = _Window(messages, budget_tokens)
    if window.fits():
        return window.tokens()
    boundary = _old_boundary(messages, keep_recent)
    for stage in (_fold_tool_outputs, _fold_arguments, _fold_replies):
        if stage(window, boundary):
            return window.tokens()
    if _truncate_messages(window, ("tool",)):
        return window.tokens()
    if _truncate_messages(window, ("user",)):
        return window.tokens()
    if _drop_batch(window) and window.fits():
        return window.tokens()
    _trim_last_unit(window)
    return window.tokens()


def task_text(messages: list[dict[str, Any]]) -> str:
    return DROPPED_STEPS_PATTERN.sub("", str(messages[1].get("content") or ""))


def pinned_report(messages: list[dict[str, Any]]) -> str:
    system = str(messages[0].get("content") or "")
    task = task_text(messages)
    return (
        f"системное сообщение {len(system)} симв. (~{estimate_tokens(system)} ток.), "
        f"задание {len(task)} симв. (~{estimate_tokens(task)} ток.)"
    )
