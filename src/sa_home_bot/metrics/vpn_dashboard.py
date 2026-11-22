from __future__ import annotations

from typing import Any

from sa_home_bot.domain.vpn_link import SCOPE_RUSSIA
from sa_home_bot.metrics import protocol
from sa_home_bot.metrics.panels import DATASOURCE, number_panels, query_variable, target

VPN_DASHBOARD_UID = "sa-home-vpn"
OK_RATIO = protocol.METRIC_VPN_CHECK_OK_RATIO
VARIABLES = (
    ("observer", "Наблюдатель", protocol.LABEL_OBSERVER),
    ("server", "Сервер", protocol.LABEL_SERVER),
    ("transport", "Способ", protocol.LABEL_TRANSPORT),
)
SELECTION = ",".join(f'{label}=~"${name}"' for name, _title, label in VARIABLES)
RUSSIA_SELECTION = f'{SELECTION},{protocol.LABEL_SCOPE}="{SCOPE_RUSSIA}"'
PAIR_LEGEND = (
    f"{{{{{protocol.LABEL_OBSERVER}}}}} → {{{{{protocol.LABEL_SERVER}}}}} · "
    f"{{{{{protocol.LABEL_TRANSPORT}}}}}"
)
PAIR_LABELS = f"{protocol.LABEL_OBSERVER}, {protocol.LABEL_SERVER}, {protocol.LABEL_TRANSPORT}"
RATIO_STEPS = [
    {"color": "red", "value": None},
    {"color": "yellow", "value": 0.0001},
    {"color": "green", "value": 0.9999},
]
RATIO_MAPPINGS = [
    {
        "type": "value",
        "options": {
            "0": {"text": "не работает", "color": "red"},
            "1": {"text": "работает", "color": "green"},
        },
    },
    {
        "type": "range",
        "options": {
            "from": 0.0001,
            "to": 0.9998,
            "result": {"text": "частично", "color": "yellow"},
        },
    },
]


def _grid(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def _matrix(title: str, selection: str, y: int) -> dict[str, Any]:
    return {
        "type": "status-history",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": _grid(0, y, 24, 10),
        "fieldConfig": {
            "defaults": {
                "min": 0,
                "max": 1,
                "unit": "percentunit",
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": RATIO_STEPS},
                "mappings": RATIO_MAPPINGS,
            },
            "overrides": [],
        },
        "options": {
            "showValue": "never",
            "rowHeight": 0.9,
            "colWidth": 0.9,
            "legend": {"showLegend": False},
        },
        "targets": [
            target(f"avg by ({PAIR_LABELS}) ({OK_RATIO}{{{selection}}})", PAIR_LEGEND, "A")
        ],
    }


def _row(title: str, y: int) -> dict[str, Any]:
    return {
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": _grid(0, y, 24, 1),
        "panels": [],
    }


def _timeseries(
    title: str, expr: str, legend: str, position: tuple[int, int], unit: str, *, percent: bool
) -> dict[str, Any]:
    defaults: dict[str, Any] = {"unit": unit}
    if percent:
        defaults.update({"min": 0, "max": 1})
    x, y = position
    return {
        "type": "timeseries",
        "title": title,
        "datasource": DATASOURCE,
        "gridPos": _grid(x, y, 12, 8),
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "targets": [target(expr, legend, "A")],
    }


def _broken_table(y: int) -> dict[str, Any]:
    columns = (
        protocol.LABEL_OBSERVER,
        protocol.LABEL_SERVER,
        protocol.LABEL_TRANSPORT,
        protocol.LABEL_TARGET,
    )
    return {
        "type": "table",
        "title": "Сейчас не работает",
        "datasource": DATASOURCE,
        "gridPos": _grid(0, y, 24, 8),
        "fieldConfig": {"defaults": {}, "overrides": []},
        "targets": [
            target(f"{protocol.METRIC_VPN_CHECK_OK}{{{SELECTION}}} == 0", "", "A", table=True)
        ],
        "transformations": [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "Value": True,
                        "job": True,
                        "instance": True,
                        protocol.LABEL_NODE: True,
                        protocol.LABEL_SCOPE: True,
                    },
                    "indexByName": {name: index for index, name in enumerate(columns)},
                    "renameByName": {
                        protocol.LABEL_OBSERVER: "Наблюдатель",
                        protocol.LABEL_SERVER: "Сервер",
                        protocol.LABEL_TRANSPORT: "Способ",
                        protocol.LABEL_TARGET: "Цель",
                    },
                },
            }
        ],
    }


def vpn_dashboard() -> dict[str, Any]:
    panels: list[dict[str, Any]] = [
        _matrix("Матрица проверок: наблюдатель → сервер · способ", SELECTION, 0),
        _row("Из России", 10),
        _matrix("Матрица проверок из России", RUSSIA_SELECTION, 11),
        _row("Доступность и задержка", 21),
        _timeseries(
            "Доступность по способам и наблюдателям (15 мин)",
            f"avg by ({protocol.LABEL_OBSERVER}, {protocol.LABEL_TRANSPORT}) "
            f"(avg_over_time({OK_RATIO}{{{SELECTION}}}[15m]))",
            f"{{{{{protocol.LABEL_OBSERVER}}}}} · {{{{{protocol.LABEL_TRANSPORT}}}}}",
            (0, 22),
            "percentunit",
            percent=True,
        ),
        _timeseries(
            "Доступность серверов (15 мин)",
            f"avg by ({protocol.LABEL_SERVER}, {protocol.LABEL_TRANSPORT}) "
            f"(avg_over_time({OK_RATIO}{{{SELECTION}}}[15m]))",
            f"{{{{{protocol.LABEL_SERVER}}}}} · {{{{{protocol.LABEL_TRANSPORT}}}}}",
            (12, 22),
            "percentunit",
            percent=True,
        ),
        _timeseries(
            "Задержка проверок, мс",
            f"avg by ({PAIR_LABELS}) ({protocol.METRIC_VPN_CHECK_LATENCY_MS}{{{SELECTION}}})",
            PAIR_LEGEND,
            (0, 30),
            "ms",
            percent=False,
        ),
        _timeseries(
            "Уровень связки (2 работает, 1 частично, 0 нет)",
            f"{protocol.METRIC_VPN_LINK_LEVEL}{{{SELECTION}}}",
            PAIR_LEGEND,
            (12, 30),
            "short",
            percent=False,
        ),
        _broken_table(38),
    ]
    number_panels(panels)
    return {
        "uid": VPN_DASHBOARD_UID,
        "title": "VPN",
        "tags": ["sa-home"],
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "templating": {
            "list": [
                query_variable(name, title, f"label_values({OK_RATIO}, {label})")
                for name, title, label in VARIABLES
            ]
        },
        "panels": panels,
    }
