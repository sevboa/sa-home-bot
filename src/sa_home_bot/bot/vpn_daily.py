from __future__ import annotations

import html
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_link_notify as notify
from sa_home_bot.bot.vpn_report_chart import MAX_MS, ChartBand, ChartPanel
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
BLIND_SHARE = 0.5
UNAVAILABLE_WORD = "недоступен"
WORKS_WORD = "работает"
HOUR_WORD = "ч"
MINUTE_WORD = "мин"

PERIOD_FORMAT = "%d.%m %H:%M"
TIME_FORMAT = "%H:%M"
PERIOD_JOIN = " – "
EPISODE_JOIN = "–"
OUTAGE_WORD = "нет связи"
ALL_PERIOD_WORD = "весь период"
BEST_WORD = "лучшее"
NO_WORKING_WORD = "рабочих подключений нет"
CAPTION_HEAD = "📊 <b>VPN за сутки</b>"
LAYERS_LEGEND = "сеть · нода · VPN"
CAUSE_SERVER = "сервер"
CAUSE_DIRECTION = "направление"
MAX_RESOURCES = 2
RESOURCE_ICON = "🌐"
RESOURCE_WORD = "не открывался ни через один сервер"
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


Observations = dict[str, dict[tuple[str, str, str], tuple[Point, ...]]]


def observations(history: dict, scopes: dict) -> Observations:
    russia = scopes.get("russia") or []
    ignored = scopes.get("ignored") or []
    seen: dict[str, dict[tuple[str, str, str], list[Point]]] = {}
    for series in history.get("series") or []:
        scope = vpn_link.scope_of(str(series.get("node") or ""), russia, ignored)
        if scope is None:
            continue
        key = (
            str(series.get("server") or ""),
            str(series.get("transport") or ""),
            str(series.get("node") or ""),
        )
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


def observers_of(group: dict[tuple[str, str, str], tuple[Point, ...]]) -> tuple[str, ...]:
    return tuple(sorted({key[2] for key in group}))


def observer_verdicts(
    group: dict[tuple[str, str, str], tuple[Point, ...]], server: str
) -> dict[str, bool]:
    totals: dict[str, list[int]] = {}
    for (key_server, _, observer), points in group.items():
        if key_server != server:
            continue
        total = totals.setdefault(observer, [0, 0])
        total[0] += sum(point.reports for point in points)
        total[1] += sum(point.down for point in points)
    return {
        observer: down / reports > BLIND_SHARE
        for observer, (reports, down) in totals.items()
        if reports
    }


def observers_diverge(verdicts: dict[str, bool]) -> bool:
    return any(verdicts.values()) and not all(verdicts.values())


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


def entry_names(history: dict) -> dict[tuple[str, str], str]:
    return {
        (str(series.get("server") or ""), str(series.get("transport") or "")): str(series["entry"])
        for series in history.get("series") or []
        if series.get("entry")
    }


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
    episodes = find_episodes(history, scopes, grid)
    names = entry_names(history)
    panels = []
    for scope in vpn_link.SCOPES:
        group = by_scope.get(scope, {})
        for server in servers:
            level = level_of(scopes, scope, server)
            if level == vpn_link.LEVEL_OK and observers_diverge(observer_verdicts(group, server)):
                level = vpn_link.LEVEL_PARTIAL
            lines = {
                (key[1], key[2]): line_values(group[key], grid)
                for key in sorted(key for key in group if key[0] == server)
            }
            panels.append(
                ChartPanel(
                    title=_panel_title(server, scope, label_of),
                    status=STATUS_TEXT.get(level, UNKNOWN_STATUS_TEXT),
                    level=level,
                    period=period,
                    x_labels=labels,
                    lines=lines,
                    names={
                        t: names[(server, t)]
                        for t in {transport for transport, _ in lines}
                        if (server, t) in names
                    },
                    observers=observers_of(group),
                    bands=tuple(
                        ChartBand(start=episode.first, end=episode.last, label=episode.cause)
                        for episode in episodes
                        if episode.scope == scope and episode.server == server
                    ),
                )
            )
    return panels


def panel_lines(history: dict, scopes: dict) -> dict[str, list[Line]]:
    by_scope = observations(history, scopes)
    merged: dict[str, dict[tuple[str, str], tuple[Point, ...]]] = {}
    for scope, group in by_scope.items():
        pairs: dict[tuple[str, str], tuple[Point, ...]] = {}
        for (server, transport, _), points in group.items():
            pairs[(server, transport)] = pairs.get((server, transport), ()) + points
        merged[scope] = pairs
    return {
        scope: [
            Line(server=pair[0], transport=pair[1], points=pairs[pair]) for pair in sorted(pairs)
        ]
        for scope, pairs in merged.items()
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
    first: int
    last: int
    start: datetime
    end: datetime
    order: int
    cause: str = ""

    @property
    def length(self) -> int:
        return self.last - self.first + 1

    def text(self, label_of: Callable[[str], str], grid: Grid) -> str:
        if self.length == grid.slots:
            span = ALL_PERIOD_WORD
        else:
            span = f"{self.start:{TIME_FORMAT}}{EPISODE_JOIN}{self.end:{TIME_FORMAT}}"
        server = _server_name(self.server, label_of)
        text = f"{notify.SCOPE_ICON[self.scope]} {server}: {OUTAGE_WORD} {span}"
        return f"{text} ({self.cause})" if self.cause else text


def _outage_slots(values_by_line: Sequence[tuple[int | None, ...]]) -> list[bool | None]:
    hits: list[bool | None] = []
    for slot in range(len(values_by_line[0])):
        values = [line[slot] for line in values_by_line if line[slot] is not None]
        hits.append(all(value == MAX_MS for value in values) if values else None)
    return hits


def _runs(hits: Sequence[bool | None]) -> list[tuple[int, int]]:
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


def episode_cause(first: int, last: int, other: Sequence[bool | None] | None) -> str:
    if other is None:
        return ""
    seen = [hit for hit in other[first : last + 1] if hit is not None]
    if not seen:
        return ""
    return CAUSE_SERVER if any(seen) else CAUSE_DIRECTION


def find_episodes(history: dict, scopes: dict, grid: Grid) -> list[Episode]:
    by_scope = observations(history, scopes)
    hits: dict[tuple[str, str], list[bool | None]] = {}
    for scope in vpn_link.SCOPES:
        group = by_scope.get(scope, {})
        for server in sorted({pair[0] for pair in group}):
            values_by_line = [
                line_values(group[key], grid) for key in sorted(group) if key[0] == server
            ]
            if values_by_line:
                hits[(scope, server)] = _outage_slots(values_by_line)

    episodes: list[Episode] = []
    for (scope, server), slots in hits.items():
        others = [hits[key] for key in hits if key[1] == server and key[0] != scope]
        for first, last in _runs(slots):
            episodes.append(
                Episode(
                    scope=scope,
                    server=server,
                    first=first,
                    last=last,
                    start=grid.starts[first],
                    end=grid.end_of(last),
                    order=len(episodes),
                    cause=episode_cause(first, last, others[0] if others else None),
                )
            )
    return episodes


def _resource_name(target: str) -> str:
    return target.split("://", 1)[-1].split("/", 1)[0]


def resource_lines(scopes: dict, *, tz: tzinfo) -> list[str]:
    lines = []
    for raw in scopes.get("resources") or []:
        if not isinstance(raw, dict):
            continue
        since, until = _moment(raw.get("since")), _moment(raw.get("until"))
        target = str(raw.get("target") or "")
        if not target or since is None or until is None:
            continue
        first = since.astimezone(tz).strftime(TIME_FORMAT)
        last = until.astimezone(tz).strftime(TIME_FORMAT)
        span = f"{first}{EPISODE_JOIN}{last}"
        name = html.escape(_resource_name(target))
        lines.append(f"{RESOURCE_ICON} {name}: {RESOURCE_WORD} {span}")
    return lines[:MAX_RESOURCES]


def outage_episodes(
    history: dict,
    scopes: dict,
    *,
    now: datetime,
    tz: tzinfo,
    label_of: Callable[[str], str],
) -> list[str]:
    grid = grid_of(history, now=now, tz=tz)
    episodes = find_episodes(history, scopes, grid)
    longest = sorted(episodes, key=lambda episode: (-episode.length, episode.order))[:MAX_EPISODES]
    texts = [
        episode.text(label_of, grid)
        for episode in sorted(longest, key=lambda episode: (episode.start, episode.order))
    ]
    return [*texts, *resource_lines(scopes, tz=tz)]


def _duration(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    parts = [f"{hours} {HOUR_WORD}"] if hours else []
    if rest or not hours:
        parts.append(f"{rest} {MINUTE_WORD}")
    return " ".join(parts)


def _down_minutes(
    group: dict[tuple[str, str, str], tuple[Point, ...]],
    server: str,
    observer: str,
    bucket_min: int,
) -> int:
    moments: dict[datetime, bool] = {}
    for (key_server, _, key_observer), points in group.items():
        if key_server != server or key_observer != observer:
            continue
        for point in points:
            moments[point.at] = moments.get(point.at, True) and point.down == point.reports
    return sum(moments.values()) * bucket_min


def _observer_text(
    group: dict[tuple[str, str, str], tuple[Point, ...]],
    server: str,
    observer: str,
    blind: bool,
    bucket_min: int,
) -> str:
    name = html.escape(observer)
    if not blind:
        return f"{name} — {WORKS_WORD}"
    minutes = _down_minutes(group, server, observer, bucket_min)
    span = f" {_duration(minutes)}" if minutes else ""
    return f"{name} — {UNAVAILABLE_WORD}{span}"


def divergence_lines(
    history: dict, scopes: dict, *, label_of: Callable[[str], str]
) -> list[str]:
    by_scope = observations(history, scopes)
    bucket_min = int(history.get("bucket_min") or DEFAULT_BUCKET_MIN)
    lines = []
    for scope in vpn_link.SCOPES:
        group = by_scope.get(scope, {})
        for server in sorted({key[0] for key in group}):
            verdicts = observer_verdicts(group, server)
            if not observers_diverge(verdicts):
                continue
            ordered = sorted(verdicts, key=lambda observer: (not verdicts[observer], observer))
            texts = ", ".join(
                _observer_text(group, server, observer, verdicts[observer], bucket_min)
                for observer in ordered
            )
            lines.append(f"{notify.SCOPE_ICON[scope]} {_server_name(server, label_of)}: {texts}")
    return lines


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
    layers = notify.layers_lines(scopes.get("layers"), label_of)
    if layers:
        head[0] = f"{CAPTION_HEAD} ({LAYERS_LEGEND})"
        head.extend(layers)
    else:
        line = notify.scopes_line(scopes.get("scopes"), label_of)
        if line:
            head.append(line)
    stats = panel_lines(history, scopes)
    for scope in vpn_link.SCOPES:
        head.append(_best_line(scope, stats.get(scope, []), label_of))

    chosen = [*divergence_lines(history, scopes, label_of=label_of), *episodes]
    tail = html.escape(conclusion) if conclusion else ""
    while chosen and len(_assemble(head, chosen, tail)) > CAPTION_LIMIT:
        chosen.pop()
    if tail and len(_assemble(head, chosen, tail)) > CAPTION_LIMIT:
        budget = CAPTION_LIMIT - len(_assemble(head, chosen, "")) - 2
        tail = clip(tail, budget) if budget > 0 else ""
    return _assemble(head, chosen, tail)
