from __future__ import annotations

import json
from typing import Any

CHARS_PER_TOKEN = 3
FOLDED_OUTPUT_PREFIX = "[вывод свёрнут"
FOLDED_OUTPUT = FOLDED_OUTPUT_PREFIX + ": {size} симв.]"
FOLDED_ARGUMENTS = '{{"свёрнуто": {size}}}'
FOLD_ARGUMENTS_MIN_CHARS = 400
PINNED_MESSAGES = 2


def estimate_tokens(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False)) // CHARS_PER_TOKEN + 1


def _foldable(messages: list[dict[str, Any]], keep_recent: int) -> list[tuple[int, int | None]]:
    tool_indexes = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    if len(tool_indexes) <= keep_recent:
        return []
    boundary = tool_indexes[-keep_recent] if keep_recent else len(messages)
    found: list[tuple[int, int | None]] = []
    for index in range(PINNED_MESSAGES, boundary):
        message = messages[index]
        if message["role"] == "tool" and not message["content"].startswith(FOLDED_OUTPUT_PREFIX):
            found.append((index, None))
        for call_index, call in enumerate(message.get("tool_calls") or ()):
            if len(call["function"]["arguments"]) >= FOLD_ARGUMENTS_MIN_CHARS:
                found.append((index, call_index))
    return found


def fold_old_results(messages: list[dict[str, Any]], budget_tokens: int, keep_recent: int) -> int:
    for index, call_index in _foldable(messages, keep_recent):
        if estimate_tokens(messages) <= budget_tokens:
            break
        message = messages[index]
        if call_index is None:
            message["content"] = FOLDED_OUTPUT.format(size=len(message["content"]))
        else:
            function = message["tool_calls"][call_index]["function"]
            function["arguments"] = FOLDED_ARGUMENTS.format(size=len(function["arguments"]))
    return estimate_tokens(messages)
