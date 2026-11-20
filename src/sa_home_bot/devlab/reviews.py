from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from sa_home_bot.devlab import protocol


@dataclass(frozen=True, order=True)
class ModelSummary:
    model: str
    jobs: int
    accepted: int
    accepted_share: float
    top_tags: list[tuple[str, int]]
    avg_tokens: int


def summarize(records: list[dict[str, Any]]) -> list[ModelSummary]:
    if not records:
        return []

    by_model: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        model = r.get(protocol.FIELD_MODEL, "")
        if model not in by_model:
            by_model[model] = []
        by_model[model].append(r)

    summaries: list[ModelSummary] = []
    for model, items in by_model.items():
        jobs = len(items)
        accepted = sum(
            1
            for r in items
            if r.get(protocol.FIELD_VERDICT) == protocol.REVIEW_ACCEPTED
        )
        accepted_share = accepted / jobs if jobs else 0.0

        tag_counter: Counter[str] = Counter()
        for r in items:
            issues = r.get(protocol.FIELD_ISSUES) or []
            for issue in issues:
                tag = issue.get(protocol.FIELD_TAG)
                if tag:
                    tag_counter[tag] += 1

        top_tags = sorted(
            tag_counter.items(),
            key=lambda x: (-x[1], x[0]),
        )[:3]

        tokens_sum = sum(r.get(protocol.PAYLOAD_TOKENS, 0) or 0 for r in items)
        avg_tokens = tokens_sum // jobs if jobs else 0

        summaries.append(
            ModelSummary(
                model=model,
                jobs=jobs,
                accepted=accepted,
                accepted_share=accepted_share,
                top_tags=top_tags,
                avg_tokens=avg_tokens,
            )
        )

    summaries.sort(key=lambda s: (-s.jobs, s.model))
    return summaries


def format_summary(rows: list[ModelSummary]) -> str:
    if not rows:
        return "журнал пуст"

    lines = ["модель, заданий, без правок %, частые метки, токенов/задание"]
    for r in rows:
        tags_str = "; ".join(f"{tag} ({count})" for tag, count in r.top_tags)
        accepted_pct = int(r.accepted_share * 100)
        lines.append(f"{r.model}, {r.jobs}, {accepted_pct}%, {tags_str}, {r.avg_tokens}")
    return "\n".join(lines)
