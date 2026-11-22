from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise

from PIL import Image, ImageDraw, ImageFont

from sa_home_bot.domain import vpn_link
from sa_home_bot.utils import fonts

log = logging.getLogger(__name__)

MAX_MS = 2000
WIDTH, HEIGHT = 1200, 700

BG = (250, 249, 245)
INK = (34, 34, 34)
MUT = (120, 120, 120)
GRID = (225, 223, 215)
BADGE_TEXT = "white"

LEVEL_COLOR = {
    vpn_link.LEVEL_OK: (60, 150, 80),
    vpn_link.LEVEL_PARTIAL: (230, 140, 40),
    vpn_link.LEVEL_DOWN: (205, 65, 55),
}
UNKNOWN_LEVEL_COLOR = MUT

TRANSPORT_COLORS = {
    "awg": (40, 110, 200),
    "awg_alt": (30, 160, 170),
    "reality": (230, 140, 40),
    "reality_alt": (150, 80, 190),
    "reality_xhttp": (200, 70, 90),
}
FALLBACK_COLORS = ((90, 90, 90), (200, 60, 120), (100, 140, 40))

GX = 90
GY = 120
PLOT_WIDTH = WIDTH - 140
PLOT_HEIGHT = 450
TOP_BAND = 56
GRID_MS = (0, 500, 1000, 1500, 2000)
TOP_BAND_FILL = (248, 226, 222)
TOP_BAND_CAPTION = "НЕТ СВЯЗИ (2 с и больше)"
TOP_BAND_CAPTION_X = GX + PLOT_WIDTH - 250
TOP_BAND_CAPTION_Y = GY + 18

BAND_FILL = (90, 90, 90, 36)
BAND_TEXT = (90, 90, 90)
BAND_CAPTION_Y = GY + PLOT_HEIGHT - 26

OUTAGE_ROW_TOP = 8
OUTAGE_ROW_STEP = 9
OUTAGE_OBSERVER_STEP = 3
SOLID: tuple[int, ...] = ()
OBSERVER_PATTERNS: tuple[tuple[int, ...], ...] = (SOLID, (14, 8), (3, 7), (14, 6, 3, 6))
DOT_RADIUS = 3
LINE_WIDTH = 4

TITLE_X, TITLE_Y = 40, 24
PERIOD_X, PERIOD_Y = 40, 72
BADGE_RIGHT = WIDTH - 40
BADGE_TOP, BADGE_BOTTOM = 28, 64
BADGE_RADIUS, BADGE_PADDING, BADGE_TEXT_Y = 8, 12, 33

GRID_LABEL_X = GX - 58
GRID_LABEL_SHIFT = -10
TIME_LABEL_Y = GY + PLOT_HEIGHT + 8

LEGEND_FIRST_TEXT_Y = 612
LEGEND_ROW_STEP = 28
LEGEND_LINE_SHIFT = 12
LEGEND_SWATCH = 34
LEGEND_TEXT_X = 42
LEGEND_STEP = 36
LEGEND_WIDTH = 6
LEGEND_RIGHT = WIDTH - 40

TITLE_SIZE = 34
BADGE_SIZE = 20
PERIOD_SIZE = 16
AXIS_SIZE = 16
BAND_SIZE = 16
TIME_SIZE = 15
LEGEND_SIZE = 18


@dataclass(frozen=True)
class ChartBand:
    start: int
    end: int
    label: str


@dataclass(frozen=True)
class ChartPanel:
    title: str
    status: str
    level: str
    period: str
    x_labels: tuple[str, ...]
    lines: dict[tuple[str, str], tuple[int | None, ...]]
    bands: tuple[ChartBand, ...] = ()
    names: dict[str, str] = field(default_factory=dict)
    observers: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Geometry:
    points: int

    def x(self, index: int) -> float:
        return GX + index * PLOT_WIDTH / max(self.points - 1, 1)

    def tick(self, index: int, total: int) -> float:
        return GX + index * PLOT_WIDTH / max(total - 1, 1)

    def y(self, value: int) -> float:
        return GY + PLOT_HEIGHT - min(value, MAX_MS) / MAX_MS * (PLOT_HEIGHT - TOP_BAND)

    def point_y(self, offset: int, value: int) -> float:
        return self.outage_y(offset) if value >= MAX_MS else self.y(value)

    def outage_y(self, offset: int) -> int:
        return GY + OUTAGE_ROW_TOP + offset


@lru_cache
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = fonts.FONT_BOLD if bold else fonts.FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        log.warning(
            "шрифт %s не найден, график без кириллицы: выполните на ноде `nodectl fix`", path
        )
        return ImageFont.load_default()


def _transports(panel: ChartPanel) -> list[str]:
    return list(dict.fromkeys(transport for transport, _ in panel.lines))


def _pattern(panel: ChartPanel, observer: str) -> tuple[int, ...]:
    if observer not in panel.observers:
        return SOLID
    return OBSERVER_PATTERNS[panel.observers.index(observer) % len(OBSERVER_PATTERNS)]


def _transport_colors(panel: ChartPanel) -> dict[str, tuple[int, int, int]]:
    colors = {}
    fallback = 0
    for transport in _transports(panel):
        if transport in TRANSPORT_COLORS:
            colors[transport] = TRANSPORT_COLORS[transport]
        else:
            colors[transport] = FALLBACK_COLORS[fallback % len(FALLBACK_COLORS)]
            fallback += 1
    return colors


def _draw_header(image: Image.Image, panel: ChartPanel) -> None:
    draw = ImageDraw.Draw(image)
    draw.text((TITLE_X, TITLE_Y), panel.title, font=_font(TITLE_SIZE, True), fill=INK)
    badge_font = _font(BADGE_SIZE, True)
    left = BADGE_RIGHT - draw.textlength(panel.status, font=badge_font) - BADGE_PADDING * 2
    draw.rounded_rectangle(
        (left, BADGE_TOP, BADGE_RIGHT, BADGE_BOTTOM),
        BADGE_RADIUS,
        fill=LEVEL_COLOR.get(panel.level, UNKNOWN_LEVEL_COLOR),
    )
    draw.text((left + BADGE_PADDING, BADGE_TEXT_Y), panel.status, font=badge_font, fill=BADGE_TEXT)
    draw.text((PERIOD_X, PERIOD_Y), panel.period, font=_font(PERIOD_SIZE), fill=MUT)


def _draw_top_band(image: Image.Image) -> None:
    ImageDraw.Draw(image).rectangle(
        (GX, GY, GX + PLOT_WIDTH, GY + TOP_BAND), fill=TOP_BAND_FILL
    )


def _draw_bands(image: Image.Image, panel: ChartPanel, geometry: _Geometry) -> None:
    font = _font(BAND_SIZE, True)
    for band in panel.bands:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle(
            (geometry.x(band.start), GY, geometry.x(band.end), GY + PLOT_HEIGHT), fill=BAND_FILL
        )
        image.paste(overlay, (0, 0), overlay)
        draw = ImageDraw.Draw(image)
        middle = (geometry.x(band.start) + geometry.x(band.end)) / 2
        left = middle - draw.textlength(band.label, font=font) / 2
        draw.text((left, BAND_CAPTION_Y), band.label, font=font, fill=BAND_TEXT)


def _draw_top_caption(image: Image.Image) -> None:
    ImageDraw.Draw(image).text(
        (TOP_BAND_CAPTION_X, TOP_BAND_CAPTION_Y),
        TOP_BAND_CAPTION,
        font=_font(TIME_SIZE, True),
        fill=LEVEL_COLOR[vpn_link.LEVEL_DOWN],
    )


def _draw_grid(image: Image.Image, geometry: _Geometry) -> None:
    draw = ImageDraw.Draw(image)
    draw.rectangle((GX, GY, GX + PLOT_WIDTH, GY + PLOT_HEIGHT), outline=GRID)
    font = _font(AXIS_SIZE)
    for ms in GRID_MS:
        y = geometry.y(ms)
        draw.line((GX, y, GX + PLOT_WIDTH, y), fill=GRID)
        label = f"{ms / 1000:g} с" if ms else "0"
        draw.text((GRID_LABEL_X, y + GRID_LABEL_SHIFT), label, font=font, fill=MUT)


def _dashes(
    points: list[tuple[float, float]], pattern: tuple[int, ...]
) -> list[list[tuple[float, float]]]:
    if not pattern:
        return [points]
    runs: list[list[tuple[float, float]]] = []
    current = [points[0]]
    index = 0
    remaining = float(pattern[0])
    for (x0, y0), (x1, y1) in pairwise(points):
        length = math.hypot(x1 - x0, y1 - y0)
        walked = 0.0
        while length - walked > remaining:
            walked += remaining
            share = walked / length
            cut = (x0 + (x1 - x0) * share, y0 + (y1 - y0) * share)
            if index % 2 == 0:
                current.append(cut)
                runs.append(current)
                current = []
            else:
                current = [cut]
            index = (index + 1) % len(pattern)
            remaining = float(pattern[index])
        remaining -= length - walked
        if index % 2 == 0:
            current.append((x1, y1))
    if index % 2 == 0 and len(current) > 1:
        runs.append(current)
    return runs


def _draw_run(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    color: tuple[int, int, int],
    pattern: tuple[int, ...] = SOLID,
) -> None:
    if not points:
        return
    if len(points) == 1:
        x, y = points[0]
        draw.ellipse(
            (x - DOT_RADIUS, y - DOT_RADIUS, x + DOT_RADIUS, y + DOT_RADIUS), fill=color
        )
        return
    for part in _dashes(points, pattern):
        draw.line(part, fill=color, width=LINE_WIDTH, joint="curve")


def _draw_lines(image: Image.Image, panel: ChartPanel, geometry: _Geometry) -> None:
    draw = ImageDraw.Draw(image)
    colors = _transport_colors(panel)
    transports = _transports(panel)
    for (transport, observer), values in panel.lines.items():
        color = colors[transport]
        pattern = _pattern(panel, observer)
        observer_order = panel.observers.index(observer) if observer in panel.observers else 0
        offset = (
            transports.index(transport) * OUTAGE_ROW_STEP
            + observer_order * OUTAGE_OBSERVER_STEP
        )
        run: list[tuple[float, float]] = []
        for index, value in enumerate(values):
            if value is None:
                _draw_run(draw, run, color, pattern)
                run = []
                continue
            run.append((geometry.x(index), geometry.point_y(offset, value)))
        _draw_run(draw, run, color, pattern)


def _draw_time_labels(image: Image.Image, panel: ChartPanel, geometry: _Geometry) -> None:
    if not panel.x_labels:
        return
    draw = ImageDraw.Draw(image)
    font = _font(TIME_SIZE)
    total = len(panel.x_labels)
    for index, label in enumerate(panel.x_labels):
        center = geometry.tick(index, total)
        draw.text(
            (center - draw.textlength(label, font=font) / 2, TIME_LABEL_Y),
            label,
            font=font,
            fill=MUT,
        )


LegendItem = tuple[str, tuple[int, int, int], tuple[int, ...]]


def _legend_items(panel: ChartPanel) -> list[list[LegendItem]]:
    colors = _transport_colors(panel)
    transports = [
        (panel.names.get(t) or t, colors[t], SOLID) for t in _transports(panel)
    ]
    present = {observer for _, observer in panel.lines}
    observers = [
        (observer, MUT, _pattern(panel, observer))
        for observer in panel.observers
        if observer in present
    ]
    return [transports, observers] if observers else [transports]


def _draw_legend(image: Image.Image, panel: ChartPanel) -> None:
    if not panel.lines:
        return
    draw = ImageDraw.Draw(image)
    font = _font(LEGEND_SIZE)
    y = LEGEND_FIRST_TEXT_Y
    for group in _legend_items(panel):
        x = GX
        for name, color, pattern in group:
            width = LEGEND_TEXT_X + draw.textlength(name, font=font)
            if x > GX and x + width > LEGEND_RIGHT:
                x = GX
                y += LEGEND_ROW_STEP
            line_y = y + LEGEND_LINE_SHIFT
            for part in _dashes([(x, line_y), (x + LEGEND_SWATCH, line_y)], pattern):
                draw.line(part, fill=color, width=LEGEND_WIDTH)
            draw.text((x + LEGEND_TEXT_X, y), name, font=font, fill=INK)
            x += width + LEGEND_STEP
        y += LEGEND_ROW_STEP


def render_panel(panel: ChartPanel) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    geometry = _Geometry(max(max((len(values) for values in panel.lines.values()), default=0), 2))
    _draw_header(image, panel)
    _draw_top_band(image)
    _draw_bands(image, panel, geometry)
    _draw_top_caption(image)
    _draw_grid(image, geometry)
    _draw_lines(image, panel, geometry)
    _draw_time_labels(image, panel, geometry)
    _draw_legend(image, panel)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
