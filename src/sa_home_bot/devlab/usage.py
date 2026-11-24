from __future__ import annotations

from typing import Any


def reasoning_tokens(usage: Any) -> int | None:
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    count = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    return None


def usage_cost(usage: Any) -> float | None:
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
