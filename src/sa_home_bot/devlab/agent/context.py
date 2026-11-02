from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

CHARS_PER_TOKEN = 3
FOLDED_OUTPUT_PREFIX = "[вывод свёрнут"
FOLDED_OUTPUT = FOLDED_OUTPUT_PREFIX + ": {size} симв.]"
FOLDED_ARGUMENTS = '{{"свёрнуто": {size}}}'
FOLD_ARGUMENTS_MIN_CHARS = 400
PINNED_MESSAGES = 2
RECENT_OUTPUT_CHARS = 2000
FOLDED_REPLY_PREFIX = "[ответ свёрнут"
FOLDED_REPLY = FOLDED_REPLY_PREFIX + ": {size} симв.]"
TRUNCATED_PREFIX = "[…обрезано"
TRUNCATED_MARK = TRUNCATED_PREFIX + " {size} симв.…]"
DROPPED_STEPS = "[свёрнуто {count} шагов]"
DROPPED_STEPS_PATTERN = re.compile(r"^\[свёрнуто (\d+) шагов\]$")

_Fits = Callable[[], bool]


def estimate_tokens(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False)) // CHARS_PER_TOKEN + 1


def _old_boundary(messages: list[dict[str, Any]], keep_recent: int) -> int:
    tool_indexes = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    if len(tool_indexes) <= keep_recent:
        return PINNED_MESSAGES
    return tool_indexes[-keep_recent] if keep_recent else len(messages)


def _fold_tool_outputs(messages: list[dict[str, Any]], boundary: int, fits: _Fits) -> bool:
    for index in range(PINNED_MESSAGES, boundary):
        message = messages[index]
        if message["role"] == "tool" and not message["content"].startswith(FOLDED_OUTPUT_PREFIX):
            message["content"] = FOLDED_OUTPUT.format(size=len(message["content"]))
            if fits():
                return True
    return False


def _fold_arguments(messages: list[dict[str, Any]], boundary: int, fits: _Fits) -> bool:
    for index in range(PINNED_MESSAGES, boundary):
        for call in messages[index].get("tool_calls") or ():
            function = call["function"]
            if len(function["arguments"]) >= FOLD_ARGUMENTS_MIN_CHARS:
                function["arguments"] = FOLDED_ARGUMENTS.format(size=len(function["arguments"]))
                if fits():
                    return True
    return False


def _fold_replies(messages: list[dict[str, Any]], boundary: int, fits: _Fits) -> bool:
    for index in range(PINNED_MESSAGES, boundary):
        message = messages[index]
        content = message.get("content")
        if (
            message["role"] == "assistant"
            and isinstance(content, str)
            and len(content) >= FOLD_ARGUMENTS_MIN_CHARS
            and not content.startswith(FOLDED_REPLY_PREFIX)
        ):
            message["content"] = FOLDED_REPLY.format(size=len(content))
            if fits():
                return True
    return False


def _truncate(content: str) -> str:
    half = RECENT_OUTPUT_CHARS // 2
    cut = len(content) - 2 * half
    return content[:half] + "\n" + TRUNCATED_MARK.format(size=cut) + "\n" + content[-half:]


def _truncate_recent(messages: list[dict[str, Any]], fits: _Fits) -> bool:
    for message in messages[PINNED_MESSAGES:]:
        content = message.get("content")
        if (
            message["role"] == "tool"
            and isinstance(content, str)
            and len(content) > RECENT_OUTPUT_CHARS
            and TRUNCATED_PREFIX not in content
        ):
            message["content"] = _truncate(content)
            if fits():
                return True
    return False


def _drop_oldest(messages: list[dict[str, Any]]) -> bool:
    count = 0
    start = PINNED_MESSAGES
    if start < len(messages) and messages[start]["role"] == "user":
        found = DROPPED_STEPS_PATTERN.match(messages[start]["content"] or "")
        if found:
            count = int(found.group(1))
            start += 1
    if start >= len(messages):
        return False
    end = start + 1
    if messages[start]["role"] == "assistant" and messages[start].get("tool_calls"):
        while end < len(messages) and messages[end]["role"] == "tool":
            end += 1
    messages[PINNED_MESSAGES:end] = [
        {"role": "user", "content": DROPPED_STEPS.format(count=count + 1)}
    ]
    return True


def fold_old_results(messages: list[dict[str, Any]], budget_tokens: int, keep_recent: int) -> int:
    def fits() -> bool:
        return estimate_tokens(messages) <= budget_tokens

    if fits():
        return estimate_tokens(messages)
    boundary = _old_boundary(messages, keep_recent)
    for stage in (_fold_tool_outputs, _fold_arguments, _fold_replies):
        if stage(messages, boundary, fits):
            return estimate_tokens(messages)
    if _truncate_recent(messages, fits):
        return estimate_tokens(messages)
    while not fits() and _drop_oldest(messages):
        pass
    return estimate_tokens(messages)


def pinned_report(messages: list[dict[str, Any]]) -> str:
    parts = []
    for label, message in zip(
        ("системное сообщение", "задание"), messages[:PINNED_MESSAGES], strict=False
    ):
        content = str(message.get("content") or "")
        parts.append(f"{label} {len(content)} симв. (~{estimate_tokens(content)} ток.)")
    return ", ".join(parts)
