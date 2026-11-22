from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sa_home_bot.metrics import protocol

NODES_DASHBOARD_UID = "sa-home-nodes"
NODE_FILTER = f'{protocol.LABEL_NODE}=~"$node"'
HOST = protocol.HOST_METRIC_PREFIX

_DATASOURCE = {"type": protocol.DATASOURCE_TYPE, "uid": protocol.DATASOURCE_UID}


def _host_expr(metric: str) -> str:
    return f"{HOST}{metric}{{{NODE_FILTER}}}"


def _target(expr: str, legend: str, ref: str, *, table: bool = False) -> dict[str, Any]:
    target: dict[str, Any] = {
        "datasource": _DATASOURCE,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
    }
    if table:
        target.update({"format": "table", "instant": True})
    return target


def _timeseries(
    title: str,
    series: list[tuple[str, str]],
    position: tuple[int, int],
    *,
    unit: str = "short",
    percent: bool = False,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit}
    if percent:
        defaults.update({"unit": "percent", "min": 0, "max": 100})
    x, y = position
    return {
        "type": "timeseries",
        "title": title,
        "datasource": _DATASOURCE,
        "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "targets": [
            _target(expr, legend, chr(ord("A") + i)) for i, (expr, legend) in enumerate(series)
        ],
    }


def _summary_table() -> dict[str, Any]:
    columns = [
        ("A", f'{protocol.METRIC_SCRAPE_UP}{{job="{protocol.JOB_NAME}",{NODE_FILTER}}}', "up", ""),
        ("B", _host_expr("cpu_busy_pct"), "CPU %", "percent"),
        ("C", _host_expr("mem_used_pct"), "RAM занято %", "percent"),
        (
            "D",
            f"max by ({protocol.LABEL_NODE}) ({_host_expr('disk_pct')})",
            "диск макс %",
            "percent",
        ),
        ("E", _host_expr("steal_pct"), "steal %", "percent"),
        ("F", _host_expr("uptime_s"), "аптайм", "s"),
        (
            "G",
            f"max by ({protocol.LABEL_NODE}) ({protocol.METRIC_TEMPERATURE}{{{NODE_FILTER}}})",
            "t макс, °C",
            "celsius",
        ),
    ]
    rename = {protocol.LABEL_NODE: "Нода"}
    rename.update({f"Value #{ref}": title for ref, _expr, title, _unit in columns})
    return {
        "type": "table",
        "title": "Сводка по нодам",
        "datasource": _DATASOURCE,
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": title},
                    "properties": [{"id": "unit", "value": unit}],
                }
                for _ref, _expr, title, unit in columns
                if unit
            ],
        },
        "targets": [_target(expr, "", ref, table=True) for ref, expr, _title, _unit in columns],
        "transformations": [
            {"id": "merge", "options": {}},
            {
                "id": "organize",
                "options": {
                    "excludeByName": {"Time": True, "job": True, "instance": True},
                    "renameByName": rename,
                },
            },
        ],
    }


def nodes_dashboard() -> dict[str, Any]:
    by_node = "{{node}}"
    specs: list[tuple[str, list[tuple[str, str]], dict[str, Any]]] = [
        ("CPU загрузка, %", [(_host_expr("cpu_busy_pct"), by_node)], {"percent": True}),
        ("CPU steal, %", [(_host_expr("steal_pct"), by_node)], {"percent": True}),
        ("CPU iowait, %", [(_host_expr("iowait_pct"), by_node)], {"percent": True}),
        ("Load на ядро", [(_host_expr("load_per_core"), by_node)], {}),
        (
            "Load average",
            [
                (_host_expr("load1"), "{{node}} 1м"),
                (_host_expr("load5"), "{{node}} 5м"),
                (_host_expr("load15"), "{{node}} 15м"),
            ],
            {},
        ),
        ("RAM занято, %", [(_host_expr("mem_used_pct"), by_node)], {"percent": True}),
        (
            "RAM доступно, байт",
            [(_host_expr("mem_available_bytes"), by_node)],
            {"unit": "bytes"},
        ),
        ("Swap занято, %", [(_host_expr("swap_used_pct"), by_node)], {"percent": True}),
        (
            "Диски по точкам монтирования, %",
            [(_host_expr("disk_pct"), "{{node}} {{mount}}")],
            {"percent": True},
        ),
        (
            "Сеть: приём, байт/с",
            [(_host_expr("net_rx_bps"), "{{node}} {{iface}}")],
            {"unit": "Bps"},
        ),
        (
            "Сеть: отдача, байт/с",
            [(_host_expr("net_tx_bps"), "{{node}} {{iface}}")],
            {"unit": "Bps"},
        ),
        ("Аптайм", [(_host_expr("uptime_s"), by_node)], {"unit": "s"}),
        ("Процессов", [(_host_expr("procs"), by_node)], {}),
        (
            "PSI (давление), %",
            [
                (_host_expr("psi_cpu"), "{{node}} cpu"),
                (_host_expr("psi_memory"), "{{node}} mem"),
                (_host_expr("psi_io"), "{{node}} io"),
            ],
            {"percent": True},
        ),
        ("conntrack, %", [(_host_expr("conntrack_pct"), by_node)], {"percent": True}),
        ("Ошибки NIC, в минуту", [(_host_expr("net_err_rate"), by_node)], {}),
        (
            "OOM-kill и стойла ядра",
            [
                (_host_expr("oom_kills"), "{{node}} oom"),
                (_host_expr("kernel_stall_events"), "{{node}} стойла"),
            ],
            {},
        ),
        (
            "Температуры, °C",
            [
                (
                    f"{protocol.METRIC_TEMPERATURE}{{{NODE_FILTER}}}",
                    "{{node}} {{component}}",
                )
            ],
            {"unit": "celsius"},
        ),
    ]
    panels = [_summary_table()]
    for index, (title, series, options) in enumerate(specs):
        position = (12 * (index % 2), 8 + 8 * (index // 2))
        panels.append(_timeseries(title, series, position, **options))
    for panel_id, panel in enumerate(panels, start=1):
        panel["id"] = panel_id
    return {
        "uid": NODES_DASHBOARD_UID,
        "title": "Ноды",
        "tags": ["sa-home"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "node",
                    "label": "Нода",
                    "type": "query",
                    "datasource": _DATASOURCE,
                    "query": {
                        "query": (
                            f"label_values({protocol.METRIC_SCRAPE_UP}"
                            f'{{job="{protocol.JOB_NAME}"}}, {protocol.LABEL_NODE})'
                        ),
                        "refId": "node",
                    },
                    "refresh": 2,
                    "multi": True,
                    "includeAll": True,
                    "current": {"selected": True, "text": "All", "value": "$__all"},
                }
            ]
        },
        "panels": panels,
    }


CATALOG: dict[str, Callable[[], dict[str, Any]]] = {"nodes.json": nodes_dashboard}


def render_dashboard(dashboard: dict[str, Any]) -> str:
    return json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n"
