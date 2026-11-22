from __future__ import annotations

from typing import Any

from sa_home_bot.metrics import protocol

DATASOURCE = {"type": protocol.DATASOURCE_TYPE, "uid": protocol.DATASOURCE_UID}


def target(expr: str, legend: str, ref: str, *, table: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "datasource": DATASOURCE,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
    }
    if table:
        result.update({"format": "table", "instant": True})
    return result


def query_variable(name: str, label: str, query: str) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "type": "query",
        "datasource": DATASOURCE,
        "query": {"query": query, "refId": name},
        "refresh": 2,
        "multi": True,
        "includeAll": True,
        "current": {"selected": True, "text": "All", "value": "$__all"},
    }


def number_panels(panels: list[dict[str, Any]]) -> None:
    for panel_id, panel in enumerate(panels, start=1):
        panel["id"] = panel_id
