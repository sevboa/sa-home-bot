from __future__ import annotations

import time

from sa_home_bot.devlab.agent.context import (
    DROPPED_STEPS_PATTERN,
    FOLDED_REPLY_PREFIX,
    RECENT_OUTPUT_CHARS,
    TRUNCATED_PATTERN,
    estimate_tokens,
    fold_old_results,
    pinned_report,
    task_text,
)
from sa_home_bot.devlab.agent.dialog import ANSWER_PREFIX
from sa_home_bot.devlab.executor.agent_worker import rules_hint

BUDGET = 13000


def pair(index: int, output: int, reply: str = "") -> list[dict]:
    call_id = f"c{index}"
    return [
        {
            "role": "assistant",
            "content": reply,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "t", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "x" * output},
    ]


def dialog(steps: int, output: int = 6000, reply: str = "") -> list[dict]:
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for index in range(steps):
        messages += pair(index, output, reply)
    return messages


def assert_valid(messages: list[dict]) -> None:
    open_ids: set[str] = set()
    for message in messages:
        if message["role"] == "assistant":
            assert not open_ids
            open_ids = {call["id"] for call in message.get("tool_calls") or ()}
        elif message["role"] == "tool":
            assert message["tool_call_id"] in open_ids
            open_ids.discard(message["tool_call_id"])
        else:
            assert not open_ids
    assert not open_ids
    assert [m["role"] for m in messages[:2]] == ["system", "user"]


def test_two_hundred_steps_converge_and_keep_pairs():
    messages = dialog(200)
    assert fold_old_results(messages, BUDGET, 4) <= BUDGET
    assert_valid(messages)
    assert DROPPED_STEPS_PATTERN.search(messages[1]["content"])
    assert messages[1]["content"].startswith("t\n\n")
    assert [m["role"] for m in messages].count("user") == 1


def test_recent_outputs_are_truncated_keeping_head_and_tail():
    messages = dialog(4, output=6000)
    messages[3]["content"] = "начало" + "x" * 5990 + "конец"
    fold_old_results(messages, 4500, 4)
    content = messages[3]["content"]
    assert content.startswith("начало") and content.endswith("конец")
    assert TRUNCATED_PATTERN.search(content)
    assert len(content) < RECENT_OUTPUT_CHARS + 100
    assert_valid(messages)


def test_large_assistant_replies_fold_before_recent_outputs():
    messages = dialog(8, output=100, reply="р" * 5000)
    fold_old_results(messages, 6000, 2)
    replies = [m["content"] for m in messages if m["role"] == "assistant"]
    assert any(r.startswith(FOLDED_REPLY_PREFIX) for r in replies)
    assert replies[-1] == "р" * 5000
    assert_valid(messages)


def test_dropped_marker_accumulates_in_task_and_pairs_stay_whole():
    messages = dialog(30, output=3000)
    fold_old_results(messages, 2500, 2)
    assert_valid(messages)
    first = int(DROPPED_STEPS_PATTERN.search(messages[1]["content"]).group(1))
    assert first > 0
    messages += pair(99, 3000) + pair(100, 3000) + pair(101, 3000)
    fold_old_results(messages, 2500, 2)
    assert_valid(messages)
    again = int(DROPPED_STEPS_PATTERN.search(messages[1]["content"]).group(1))
    assert again > first
    assert messages[1]["content"].count("[свёрнуто") == 1
    assert task_text(messages) == "t"


def test_last_unit_is_never_dropped_only_trimmed():
    messages = dialog(3, output=100)
    huge = pair(9, 50000)
    huge[0]["tool_calls"][0]["function"]["arguments"] = "y" * 30000
    messages += huge
    assert fold_old_results(messages, 2500, 1) <= 2500
    assert_valid(messages)
    assert messages[-2]["tool_calls"][0]["id"] == "c9"
    assert TRUNCATED_PATTERN.search(messages[-1]["content"])


def test_several_tool_calls_in_one_unit_stay_together():
    messages = dialog(0)
    for index in range(10):
        first, second = pair(index * 2, 3000), pair(index * 2 + 1, 3000)
        first[0]["tool_calls"] += second[0]["tool_calls"]
        messages += first + [second[1]]
    assert fold_old_results(messages, 2500, 1) <= 2500
    assert_valid(messages)


def test_owner_answers_are_never_dropped_only_compressed():
    messages = dialog(10, output=3000)
    answer = {"role": "user", "content": ANSWER_PREFIX + "а" * 9000}
    messages.insert(4, answer)
    fold_old_results(messages, 2500, 1)
    assert_valid(messages)
    kept = [m for m in messages if str(m["content"]).startswith(ANSWER_PREFIX)]
    assert len(kept) == 1
    assert len(kept[0]["content"]) < 9000


def test_thousand_steps_fold_fast():
    messages = dialog(1000)
    started = time.monotonic()
    assert fold_old_results(messages, BUDGET, 4) <= BUDGET
    assert time.monotonic() - started < 2
    assert_valid(messages)


def test_rules_hint_empty_and_filled():
    assert rules_hint([]) == ""
    assert "RULES.md: 5 симв." in rules_hint([("RULES.md", "abcde")])


def test_folding_is_idempotent_and_noop_when_fits():
    messages = dialog(3, output=100)
    before = [dict(m) for m in messages]
    fold_old_results(messages, BUDGET, 4)
    assert messages == before


def test_pinned_alone_over_budget_stays_over_and_reports_sizes():
    messages = dialog(5)
    messages[0]["content"] = "с" * 3000
    messages[1]["content"] = "з" * 60000
    assert fold_old_results(messages, 2000, 4) > 2000
    assert_valid(messages)
    assert "[свёрнуто" not in task_text(messages)
    report = pinned_report(messages)
    assert "3000 симв." in report and "60000 симв." in report
    assert estimate_tokens("з" * 60000) > 2000
