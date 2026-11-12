from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from sa_home_bot.bot import vpn_daily as daily
from sa_home_bot.bot.vpn_report_chart import MAX_MS

TZ = ZoneInfo("Asia/Almaty")
NOW = datetime(2026, 11, 12, 3, 20, tzinfo=UTC)
FIRST = datetime(2026, 11, 11, 3, 30, tzinfo=UTC)
SLOTS = 96
TITLE_GLOBAL = "NL · jeeves · Глобально"
TITLE_RUSSIA = "NL · jeeves · Из России"

LABELS = {"jeeves": "🇳🇱 Нидерланды", "wooster": "🇺🇸 США"}
SCOPES = {
    "scopes": {"global": {"jeeves": "ok"}, "russia": {"wooster": "partial", "zee": "down"}},
    "russia": ["osip"],
    "ignored": ["zakhar"],
    "links": [],
}
EMPTY_SCOPES = {"scopes": {}, "russia": [], "ignored": []}


def label_of(server: str) -> str:
    return LABELS.get(server, "")


def _at(slot: int) -> str:
    return (FIRST + timedelta(minutes=15 * slot)).isoformat()


def _point(slot: int, reports: int, down: int, median_ms: int | None) -> dict:
    return {"at": _at(slot), "reports": reports, "down": down, "median_ms": median_ms}


def _series(node: str, server: str, transport: str, points: list[dict]) -> dict:
    return {"node": node, "server": server, "transport": transport, "points": points}


def _history(*series: dict) -> dict:
    return {"hours": 24, "bucket_min": 15, "series": list(series)}


def _panels(history: dict, scopes: dict = SCOPES) -> list:
    return daily.build_panels(history, scopes, now=NOW, tz=TZ, label_of=label_of)


def _panel(panels: list, title: str):
    return next(panel for panel in panels if panel.title == title)


def _p(reports: int, down: int, median_ms: int | None) -> daily.Point:
    return daily.Point(at=FIRST, reports=reports, down=down, median_ms=median_ms)


def _line(server: str, transport: str, points: list[daily.Point]) -> daily.Line:
    return daily.Line(server=server, transport=transport, points=tuple(points))


def test_line_length_is_slots_and_point_falls_into_its_slot():
    points = [_point(95, 3, 0, 612), _point(94, 2, 2, 700)]
    panels = _panels(_history(_series("osip", "jeeves", "awg", points)))
    values = _panel(panels, TITLE_RUSSIA).lines["awg"]
    assert len(values) == SLOTS
    assert values[95] == 612
    assert values[94] == MAX_MS
    assert values[93] is None


def test_point_outside_the_grid_is_dropped():
    off_grid = {"at": "2026-11-12T03:20:00+00:00", "reports": 1, "down": 0, "median_ms": 100}
    panels = _panels(_history(_series("osip", "jeeves", "awg", [off_grid])))
    values = _panel(panels, TITLE_RUSSIA).lines["awg"]
    assert len(values) == SLOTS
    assert all(value is None for value in values)


def test_ignored_node_is_skipped_and_nodes_are_split_by_scope():
    history = _history(
        _series("mike", "jeeves", "reality", [_point(0, 2, 0, 300)]),
        _series("osip", "jeeves", "reality", [_point(0, 4, 1, 400)]),
        _series("zakhar", "jeeves", "reality", [_point(0, 10, 10, 100)]),
    )
    panels = _panels(history)
    assert _panel(panels, TITLE_GLOBAL).lines["reality"][0] == 300
    assert _panel(panels, TITLE_RUSSIA).lines["reality"][0] == 400
    assert sorted(_panel(panels, TITLE_GLOBAL).lines) == ["reality"]
    assert sorted(_panel(panels, TITLE_RUSSIA).lines) == ["reality"]


def test_slot_with_all_down_is_max_ms_and_empty_slot_is_none():
    history = _history(_series("osip", "jeeves", "awg", [_point(1, 3, 3, 700)]))
    values = _panel(_panels(history), TITLE_RUSSIA).lines["awg"]
    assert values[1] == MAX_MS
    assert values[0] is None


def test_panels_are_ordered_by_scope_then_server():
    history = _history(
        _series("osip", "wooster", "awg", [_point(0, 1, 0, 100)]),
        _series("mike", "jeeves", "reality", [_point(0, 1, 0, 200)]),
    )
    panels = _panels(history)
    assert [panel.title for panel in panels] == [
        TITLE_GLOBAL,
        "US · wooster · Глобально",
        "zee · Глобально",
        TITLE_RUSSIA,
        "US · wooster · Из России",
        "zee · Из России",
    ]
    assert _panel(panels, TITLE_GLOBAL).lines == {"reality": (200,) + (None,) * (SLOTS - 1)}
    assert _panel(panels, "zee · Глобально").lines == {}


@pytest.mark.parametrize(
    ("flag", "code"),
    [
        ("🇳🇱", "NL"),
        ("🇺🇸", "US"),
        ("🌍", ""),
        ("", ""),
        ("Нидерланды", ""),
    ],
)
def test_country_code_only_from_two_regional_indicators(flag: str, code: str) -> None:
    assert daily.country_code(flag) == code


def test_title_without_country_code_when_flag_is_not_two_letters():
    history = _history(_series("mike", "solar", "awg", [_point(0, 1, 0, 100)]))
    assert _panel(_panels(history), "solar · Глобально").title == "solar · Глобально"


@pytest.mark.parametrize(
    ("title", "level", "status"),
    [
        (TITLE_GLOBAL, "ok", "всё работает"),
        ("US · wooster · Из России", "partial", "частично"),
        ("zee · Из России", "down", "нет связи"),
        (TITLE_RUSSIA, "", "нет данных"),
    ],
)
def test_status_and_level_per_scope_and_server(title: str, level: str, status: str) -> None:
    panel = _panel(_panels(_history()), title)
    assert (panel.level, panel.status) == (level, status)


def test_period_and_x_labels_in_time_zone():
    panels = _panels(_history())
    assert panels[0].period == "11.11 08:30 – 12.11 08:30"
    assert panels[0].x_labels == (
        "08:30",
        "11:30",
        "14:30",
        "17:30",
        "20:30",
        "23:30",
        "02:30",
        "05:30",
        "08:30",
    )
    assert all(panel.period == panels[0].period for panel in panels)
    assert all(panel.x_labels == panels[0].x_labels for panel in panels)


def test_best_connection_prefers_biggest_share():
    best = daily.best_connection([
        _line("wooster", "reality", [_p(20, 2, 500)]),
        _line("jeeves", "awg", [_p(10, 1, 540)]),
    ])
    assert (best.server, best.transport, best.median_ms) == ("wooster", "reality", 500)
    assert round(best.share * 100) == 90


def _tie(a: daily.Line, b: daily.Line) -> list[daily.Line]:
    return [a, b]


def _tie_line(server: str, transport: str, median: int | None) -> daily.Line:
    return _line(server, transport, [_p(10, 1, median)])


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (_tie(_tie_line("zee", "awg", 700), _tie_line("zee", "reality", 600)), "reality"),
        (_tie(_tie_line("zee", "awg", None), _tie_line("zee", "reality", 600)), "reality"),
        (_tie(_tie_line("zee", "reality", 600), _tie_line("aee", "awg", 600)), "awg"),
        (_tie(_tie_line("zee", "reality", 600), _tie_line("zee", "awg", 600)), "awg"),
    ],
)
def test_best_connection_ties_are_broken_by_median_then_names(lines: list, expected: str) -> None:
    assert daily.best_connection(lines).transport == expected


def test_best_connection_without_points():
    assert daily.best_connection([]) is None
    assert daily.best_connection([_line("jeeves", "awg", [])]) is None


def test_caption_best_lines_and_no_working_connections():
    history = _history(
        _series("mike", "jeeves", "awg", [_point(0, 100, 1, 540)]),
        _series("osip", "wooster", "reality", [_point(0, 10, 10, None)]),
    )
    caption = daily.build_caption(history, SCOPES, label_of=label_of)
    assert caption.split("\n") == [
        "📊 <b>VPN за сутки</b>",
        "🌍 🇳🇱🟢 · 🇷🇺 🇺🇸🟡 zee🔴",
        "🌍 лучшее: 🇳🇱 jeeves · AmneziaVPN — 99 %, 540 мс",
        "🇷🇺 рабочих подключений нет",
    ]


def test_caption_without_median_flag_and_with_escaping():
    history = _history(_series("mike", "a<b", "zzz", [_point(0, 4, 1, None)]))
    caption = daily.build_caption(history, EMPTY_SCOPES, label_of=label_of)
    assert caption.split("\n") == [
        "📊 <b>VPN за сутки</b>",
        "🌍 лучшее: a&lt;b · zzz — 75 %",
        "🇷🇺 рабочих подключений нет",
    ]


def test_caption_conclusion_goes_after_blank_line():
    caption = daily.build_caption(
        _history(), EMPTY_SCOPES, label_of=label_of, conclusion="итог <ok>"
    )
    assert caption.endswith("\n\nитог &lt;ok&gt;")
    short = daily.build_caption(_history(), EMPTY_SCOPES, label_of=label_of, conclusion="")
    assert "\n\n" not in short


def test_outage_episodes_take_longest_runs_in_time_order():
    points = []
    for first, length in ((10, 3), (20, 5), (30, 2), (40, 4)):
        points.extend(_point(slot, 1, 1, None) for slot in range(first, first + length))
    history = _history(_series("osip", "jeeves", "awg", points))
    episodes = daily.outage_episodes(history, SCOPES, now=NOW, tz=TZ, label_of=label_of)
    assert episodes == [
        "🇷🇺 🇳🇱 jeeves: нет связи 11:00–11:45",
        "🇷🇺 🇳🇱 jeeves: нет связи 13:30–14:45",
        "🇷🇺 🇳🇱 jeeves: нет связи 18:30–19:30",
    ]


def test_outage_episode_covering_whole_period():
    points = [_point(slot, 1, 1, None) for slot in range(SLOTS)]
    history = _history(_series("osip", "jeeves", "awg", points))
    episodes = daily.outage_episodes(history, SCOPES, now=NOW, tz=TZ, label_of=label_of)
    assert episodes == ["🇷🇺 🇳🇱 jeeves: нет связи весь период"]


def test_outage_episode_needs_a_value_and_all_values_down():
    scopes = {
        "scopes": {"russia": {"jeeves": "partial"}},
        "russia": ["osip", "peter"],
        "ignored": [],
    }
    mixed = _history(
        _series("osip", "jeeves", "awg", [_point(5, 1, 1, None)]),
        _series("peter", "jeeves", "reality", [_point(5, 1, 0, 300)]),
    )
    assert daily.outage_episodes(mixed, scopes, now=NOW, tz=TZ, label_of=label_of) == []
    empty = _history(_series("osip", "jeeves", "awg", []))
    assert daily.outage_episodes(empty, SCOPES, now=NOW, tz=TZ, label_of=label_of) == []


def test_caption_is_clipped_and_keeps_conclusion():
    episodes = [f"🇷🇺 🇳🇱 jeeves: нет связи {h:02d}:00–{h:02d}:15" for h in range(20, 30)]
    conclusion = "x" * 950
    caption = daily.build_caption(
        _history(),
        EMPTY_SCOPES,
        label_of=label_of,
        conclusion=conclusion,
        episodes=episodes,
    )
    assert len(caption) <= daily.CAPTION_LIMIT
    lines = caption.split("\n")
    assert lines[0] == "📊 <b>VPN за сутки</b>"
    assert lines[1] == "🌍 рабочих подключений нет"
    assert lines[2] == "🇷🇺 рабочих подключений нет"
    assert lines[3] == ""
    assert 0 < len(lines[4]) < len(conclusion)
    assert not any("нет связи" in line for line in lines)


def test_caption_keeps_episodes_when_it_fits():
    episodes = ["🇷🇺 🇳🇱 jeeves: нет связи 11:00–11:45", "🌍 jeeves: нет связи 13:00–14:00"]
    caption = daily.build_caption(
        _history(),
        EMPTY_SCOPES,
        label_of=label_of,
        conclusion="короткий итог",
        episodes=episodes,
    )
    assert caption.split("\n") == [
        "📊 <b>VPN за сутки</b>",
        "🌍 рабочих подключений нет",
        "🇷🇺 рабочих подключений нет",
        "🇷🇺 🇳🇱 jeeves: нет связи 11:00–11:45",
        "🌍 jeeves: нет связи 13:00–14:00",
        "",
        "короткий итог",
    ]
