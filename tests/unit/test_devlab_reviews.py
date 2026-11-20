from __future__ import annotations

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.reviews import ModelSummary, format_summary, summarize


def test_summarize_empty():
    assert summarize([]) == []


@pytest.mark.parametrize(
    "records,expected",
    [
        (
            [
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [],
                    protocol.PAYLOAD_TOKENS: 100,
                },
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED_WITH_FIXES,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_LOOP, protocol.FIELD_NOTE: "x"}
                    ],
                    protocol.PAYLOAD_TOKENS: 200,
                },
                {
                    protocol.FIELD_MODEL: "model-b",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_MISSED_CASE, protocol.FIELD_NOTE: "y"}
                    ],
                    protocol.PAYLOAD_TOKENS: 300,
                },
            ],
            [
                ModelSummary(
                    model="model-a",
                    jobs=2,
                    accepted=1,
                    accepted_share=0.5,
                    top_tags=[(protocol.ISSUE_LOOP, 1)],
                    avg_tokens=150,
                ),
                ModelSummary(
                    model="model-b",
                    jobs=1,
                    accepted=1,
                    accepted_share=1.0,
                    top_tags=[(protocol.ISSUE_MISSED_CASE, 1)],
                    avg_tokens=300,
                ),
            ],
        ),
        (
            [
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_LOOP, protocol.FIELD_NOTE: "x"}
                    ],
                    protocol.PAYLOAD_TOKENS: 100,
                },
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_LOOP, protocol.FIELD_NOTE: "x"}
                    ],
                    protocol.PAYLOAD_TOKENS: 200,
                },
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_REDONE,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: protocol.ISSUE_MISSED_CASE, protocol.FIELD_NOTE: "y"}
                    ],
                    protocol.PAYLOAD_TOKENS: 300,
                },
            ],
            [
                ModelSummary(
                    model="model-a",
                    jobs=3,
                    accepted=2,
                    accepted_share=2 / 3,
                    top_tags=[(protocol.ISSUE_LOOP, 2), (protocol.ISSUE_MISSED_CASE, 1)],
                    avg_tokens=200,
                )
            ],
        ),
        (
            [
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: "a", protocol.FIELD_NOTE: ""},
                        {protocol.FIELD_TAG: "b", protocol.FIELD_NOTE: ""},
                    ],
                    protocol.PAYLOAD_TOKENS: 100,
                },
                {
                    protocol.FIELD_MODEL: "model-a",
                    protocol.FIELD_VERDICT: protocol.REVIEW_ACCEPTED,
                    protocol.FIELD_ISSUES: [
                        {protocol.FIELD_TAG: "a", protocol.FIELD_NOTE: ""},
                        {protocol.FIELD_TAG: "c", protocol.FIELD_NOTE: ""},
                    ],
                    protocol.PAYLOAD_TOKENS: 200,
                },
            ],
            [
                ModelSummary(
                    model="model-a",
                    jobs=2,
                    accepted=2,
                    accepted_share=1.0,
                    top_tags=[("a", 2), ("b", 1), ("c", 1)],
                    avg_tokens=150,
                )
            ],
        ),
    ],
)
def test_summarize(records, expected):
    assert summarize(records) == expected


def test_format_summary_empty():
    assert format_summary([]) == "журнал пуст"


def test_format_summary():
    rows = [
        ModelSummary(
            model="model-a",
            jobs=2,
            accepted=1,
            accepted_share=0.5,
            top_tags=[("loop", 1)],
            avg_tokens=150,
        ),
        ModelSummary(
            model="model-b",
            jobs=1,
            accepted=1,
            accepted_share=1.0,
            top_tags=[("missed_case", 1)],
            avg_tokens=300,
        ),
    ]
    out = format_summary(rows)
    lines = out.splitlines()
    assert lines[0] == "модель, заданий, без правок %, частые метки, токенов/задание"
    assert lines[1] == "model-a, 2, 50%, loop (1), 150"
    assert lines[2] == "model-b, 1, 100%, missed_case (1), 300"
