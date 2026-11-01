from __future__ import annotations

from typing import Any


def usage_cost(usage: Any) -> float | None:
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None
