from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_link_notify as notify
from sa_home_bot.bot.vpn_report_chart import MAX_MS, ChartPanel
from sa_home_bot.domain import vpn_link
from sa_home_bot.domain.vpn_history import bucket_start, median_ms
from sa_home_bot.utils.text import clip

CAPTION_LIMIT = 1024
MAX_EPISODES = 3
DEFAULT_HOURS = 24
DEFAULT_BUCKET_MIN = 15
X_LABEL_STEP_HOURS = 3

SCOPE_TITLE = {vpn_link.SCOPE_GLOBAL: "Глобально", vpn_link.SCOPE_RUSSIA: "Из России"}
STATUS_TEXT = {
    vpn_link.LEVEL_OK: "всё работает",
    vpn_link.LEVEL_PARTIAL: "частично",
    vpn_link.LEVEL_DOWN: "нет связи",
}
UNKNOWN_STATUS_TEXT = "нет данных"

PERIOD_FORMAT = "%d.%m %H:%M"
TIME_FORMAT = "%H:%M"
PERIOD_JOIN = " – "
EPISODE_JOIN = "–"
OUTAGE_WORD = "нет связи"
ALL_PERIOD_WORD = "весь период"
BEST_WORD = "лучшее"
NO_WORKING_WORD = "рабочих подключений нет"
CAPTION_HEAD = "📊 <b>VPN за сутки</b>"
PERCENT_JOIN = " %"
MS_JOIN = " мс"
REGIONAL_INDICATOR_FIRST = 0x1F1E6
REGIONAL_INDICATOR_LAST = 0x1F1FF


def country_code(flag: str) -> str:
    if len(flag) != 2:
        return ""
    if not all(REGIONAL_INDICATOR_FIRST <= ord(char) <= REGIONAL_INDICATOR_LAST for char in flag):
        return ""
    return "".join(chr(ord("A") + ord(char) - REGIONAL_INDICATOR_FIRST) for char in flag)


@dataclass(frozen=True)
class Point:
    at: datetime
    reports: int
    down: int
    median_ms: int | None


def _moment(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _point(raw: dict, at: datetime) -> Point:
    return Point(
        at=at,
        reports=int(raw.get("reports") or 0),
        down=int(raw.get("down") or 0),
        median_ms=raw.get("median_ms"),
    )


def _median_of(points: Iterable[Point]) -> int | None:
    return median_ms(p.median_ms for p in points if p.down < p.reports and p.median_ms is not None)


@dataclass(frozen=True)
class Line:
    server: str
    transport: str
    points: tuple[Point, ...]

    @property
    def reports(self) -> int:
        return sum(point.reports for point in self.points)

    @property
    def down(self) -> int:
        return sum(point.down for point in self.points)

    @property
    def share(self) -> float:
        reports = self.reports
        return 0.0 if not reports else 1 - self.down / reports

    @property
    def median_ms(self) -> int | None:
        return _median_of(self.points)


@dataclass(frozen=True)
class Grid:
    bucket_s: int
    slots: int
    starts: tuple[datetime, ...]
    slot_of: dict[int, int]

    def end_of(self, slot: int) -> datetime:
        return self.starts[slot] + timedelta(seconds=self.bucket_s)

    def slot(self, at: datetime) -> int | None:
        return self.slot_of.get(int(at.timestamp()))


def grid_of(history: dict, *, now: datetime, tz: tzinfo) -> Grid:
    bucket_min = int(history.get("bucket_min") or DEFAULT_BUCKET_MIN)
    bucket_s = bucket_min * 60
    slots = int(history.get("hours") or DEFAULT_HOURS) * 60 // bucket_min
    last = bucket_start(now, bucket_s).astimezone(tz)
    starts = tuple(last - timedelta(seconds=(slots - 1 - i) * bucket_s) for i in range(slots))
    return Grid(
        bucket_s=bucket_s,
        slots=slots,
        starts=starts,
        slot_of={int(start.timestamp()): index for index, start in enumerate(starts)},
    )


Observations = dict[str, dict[tuple[str, str], tuple[Point, ...]]]


def observations(history: dict, scopes: dict) -> Observations:
    russia = scopes.get("russia") or []
    ignored = scopes.get("ignored") or []
    seen: dict[str, dict[tuple[str, str], list[Point]]] = {}
    for series in history.get("series") or []:
        scope = vpn_link.scope_of(str(series.get("node") or ""), russia, ignored)
        if scope is None:
            continue
        key = (str(series.get("server") or ""), str(series.get("transport") or ""))
        points = seen.setdefault(scope, {}).setdefault(key, [])
        for raw in series.get("points") or []:
            at = _moment(raw.get("at"))
            if at is not None:
                points.append(_point(raw, at))
    grouped = {}
    for scope in sorted(seen):
        grouped[scope] = {key: tuple(seen[scope][key]) for key in sorted(seen[scope])}
    return grouped


def line_values(points: Sequence[Point], grid: Grid) -> tuple[int | None, ...]:
    by_slot: dict[int, list[Point]] = {}
    for point in points:
        slot = grid.slot(point.at)
        if slot is not None:
            by_slot.setdefault(slot, []).append(point)

    values = []
    for slot in range(grid.slots):
        group = by_slot.get(slot) or []
        if not group:
            values.append(None)
        elif all(point.down == point.reports for point in group):
            values.append(MAX_MS)
        else:
            values.append(_median_of(group))
    return tuple(values)


def servers_of(history: dict, scopes: dict) -> list[str]:
    collected = {str(series.get("server") or "") for series in history.get("series") or []}
    for levels in (scopes.get("scopes") or {}).values():
        collected.update(str(server) for server in levels)
    return sorted(server for server in collected if server)


def level_of(scopes: dict, scope: str, server: str) -> str:
    return str((scopes.get("scopes") or {}).get(scope, {}).get(server) or "")


def _period(grid: Grid) -> str:
    if not grid.starts:
        return ""
    first = grid.starts[0].strftime(PERIOD_FORMAT)
    last = grid.end_of(grid.slots - 1).strftime(PERIOD_FORMAT)
    return f"{first}{PERIOD_JOIN}{last}"


def _x_labels(grid: Grid, hours: int) -> tuple[str, ...]:
    if not grid.starts:
        return ()
    first = grid.starts[0]
    step = timedelta(hours=X_LABEL_STEP_HOURS)
    return tuple(
        (first + k * step).strftime(TIME_FORMAT) for k in range(hours // X_LABEL_STEP_HOURS + 1)
    )


def _panel_title(server: str, scope: str, label_of: Callable[[str], str]) -> str:
    title = SCOPE_TITLE[scope]
    code = country_code(notify.server_flag(label_of(server)))
    return f"{code} · {server} · {title}" if code else f"{server} · {title}"


def build_panels(
    history: dict,
    scopes: dict,
    *,
    now: datetime,
    tz: tzinfo,
    label_of: Callable[[str], str],
) -> list[ChartPanel]:
    grid = grid_of(history, now=now, tz=tz)
    by_scope = observations(history, scopes)
    servers = servers_of(history, scopes)
    period = _period(grid)
    labels = _x_labels(grid, int(history.get("hours") or DEFAULT_HOURS))
    panels = []
    for scope in vpn_link.SCOPES:
        group = by_scope.get(scope, {})
        for server in servers:
            level = level_of(scopes, scope, server)
            lines = {
                transport: line_values(group[(server, transport)], grid)
                for transport in sorted(key[1] for key in group if key[0] == server)
            }
            panels.append(
                ChartPanel(
                    title=_panel_title(server, scope, label_of),
                    status=STATUS_TEXT.get(level, UNKNOWN_STATUS_TEXT),
                    level=level,
                    period=period,
                    x_labels=labels,
                    lines=lines,
                )
            )
    return panels


def panel_lines(history: dict, scopes: dict) -> dict[str, list[Line]]:
    by_scope = observations(history, scopes)
    return {
        scope: [
            Line(server=pair[0], transport=pair[1], points=by_scope[scope][pair])
            for pair in sorted(by_scope[scope])
        ]
        for scope in by_scope
    }


def best_connection(panels_lines: Sequence[Line]) -> Line | None:
    candidates = [line for line in panels_lines if line.points and line.reports]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda line: (
            -line.share,
            line.median_ms is None,
            line.median_ms or 0,
            line.server,
            line.transport,
        ),
    )


def _server_name(server: str, label_of: Callable[[str], str]) -> str:
    flag = notify.server_flag(label_of(server))
    escaped = html.escape(server)
    return f"{flag} {escaped}" if flag else escaped


def _best_line(scope: str, lines: Sequence[Line], label_of: Callable[[str], str]) -> str:
    best = best_connection(lines)
    icon = notify.SCOPE_ICON[scope]
    if best is None or best.share <= 0:
        return f"{icon} {NO_WORKING_WORD}"
    transport = html.escape(vd.TECHNICAL_TRANSPORT_NAME.get(best.transport, best.transport))
    text = (
        f"{icon} {BEST_WORD}: {_server_name(best.server, label_of)} · {transport} — "
        f"{round(best.share * 100)}{PERCENT_JOIN}"
    )
    if best.median_ms is not None:
        text = f"{text}, {best.median_ms}{MS_JOIN}"
    return text


@dataclass(frozen=True)
class Episode:
    scope: str
    server: str
    start: datetime
    end: datetime
    length: int
    order: int

    def text(self, label_of: Callable[[str], str], grid: Grid) -> str:
        if self.length == grid.slots:
            span = ALL_PERIOD_WORD
        else:
            span = f"{self.start:{TIME_FORMAT}}{EPISODE_JOIN}{self.end:{TIME_FORMAT}}"
        server = _server_name(self.server, label_of)
        return f"{notify.SCOPE_ICON[self.scope]} {server}: {OUTAGE_WORD} {span}"


def _outage_slots(values_by_line: Sequence[tuple[int | None, ...]]) -> list[bool]:
    hits = []
    for slot in range(len(values_by_line[0])):
        values = [line[slot] for line in values_by_line]
        hits.append(
            any(value is not None for value in values)
            and all(value == MAX_MS for value in values if value is not None)
        )
    return hits


def _runs(hits: Sequence[bool]) -> list[tuple[int, int]]:
    runs = []
    start = None
    for slot, hit in enumerate(hits):
        if hit and start is None:
            start = slot
        elif not hit and start is not None:
            runs.append((start, slot - 1))
            start = None
    if start is not None:
        runs.append((start, len(hits) - 1))
    return runs


def outage_episodes(
    history: dict,
    scopes: dict,
    *,
    now: datetime,
    tz: tzinfo,
    label_of: Callable[[str], str],
) -> list[str]:
    grid = grid_of(history, now=now, tz=tz)
    by_scope = observations(history, scopes)
    episodes: list[Episode] = []
    for scope in vpn_link.SCOPES:
        group = by_scope.get(scope, {})
        for server in sorted({pair[0] for pair in group}):
            values_by_line = [
                line_values(group[(server, transport)], grid)
                for transport in sorted(key[1] for key in group if key[0] == server)
            ]
            if not values_by_line:
                continue
            for first, last in _runs(_outage_slots(values_by_line)):
                episodes.append(
                    Episode(
                        scope=scope,
                        server=server,
                        start=grid.starts[first],
                        end=grid.end_of(last),
                        length=last - first + 1,
                        order=len(episodes),
                    )
                )

    longest = sorted(episodes, key=lambda episode: (-episode.length, episode.order))[:MAX_EPISODES]
    return [
        episode.text(label_of, grid)
        for episode in sorted(longest, key=lambda episode: (episode.start, episode.order))
    ]


def _assemble(head: Sequence[str], episodes: Sequence[str], conclusion: str) -> str:
    parts = [*head, *episodes]
    if conclusion:
        parts.extend(["", conclusion])
    return "\n".join(parts)


def build_caption(
    history: dict,
    scopes: dict,
    *,
    label_of: Callable[[str], str],
    conclusion: str | None = None,
    episodes: Sequence[str] = (),
) -> str:
    head = [CAPTION_HEAD]
    line = notify.scopes_line(scopes.get("scopes"), label_of)
    if line:
        head.append(line)
    stats = panel_lines(history, scopes)
    for scope in vpn_link.SCOPES:
        head.append(_best_line(scope, stats.get(scope, []), label_of))

    chosen = list(episodes)
    tail = html.escape(conclusion) if conclusion else ""
    while chosen and len(_assemble(head, chosen, tail)) > CAPTION_LIMIT:
        chosen.pop()
    if tail and len(_assemble(head, chosen, tail)) > CAPTION_LIMIT:
        budget = CAPTION_LIMIT - len(_assemble(head, chosen, "")) - 2
        tail = clip(tail, budget) if budget > 0 else ""
    return _assemble(head, chosen, tail)
