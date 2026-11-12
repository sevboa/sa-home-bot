from __future__ import annotations

import io

from PIL import Image

from sa_home_bot.bot import vpn_report_chart as chart
from sa_home_bot.domain import vpn_link


def _open(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    return image


def _colors(png: bytes) -> set[tuple[int, int, int]]:
    return set(_open(png).convert("RGB").getdata())


def _panel(**options) -> chart.ChartPanel:
    base = {
        "title": "NL · jeeves · Глобально",
        "status": "всё работает",
        "level": vpn_link.LEVEL_OK,
        "period": "сутки",
        "x_labels": ("00:00", "06:00", "12:00", "18:00", "24:00"),
        "lines": {},
    }
    base.update(options)
    return chart.ChartPanel(**base)


def _values(base: int, points: int) -> tuple[int, ...]:
    return tuple(base + (index % 7) * 20 for index in range(points))


def test_panel_is_png_of_expected_size():
    png = chart.render_panel(
        _panel(lines={"awg": _values(600, 96), "reality": _values(900, 96)})
    )
    assert _open(png).size == (chart.WIDTH, chart.HEIGHT)


def test_lines_are_drawn_in_transport_colors():
    png = chart.render_panel(
        _panel(lines={"awg": _values(600, 96), "reality": _values(900, 96)})
    )
    colors = _colors(png)
    assert chart.TRANSPORT_COLORS["awg"] in colors
    assert chart.TRANSPORT_COLORS["reality"] in colors


def test_line_of_max_ms_is_drawn_in_top_band():
    png = chart.render_panel(_panel(lines={"awg": _values(chart.MAX_MS, 96)}))
    image = _open(png).convert("RGB")
    color = chart.TRANSPORT_COLORS["awg"]
    rows = [
        y
        for y in range(chart.GY, chart.GY + chart.PLOT_HEIGHT)
        for x in range(chart.GX, chart.GX + chart.PLOT_WIDTH)
        if image.getpixel((x, y)) == color
    ]
    assert rows
    assert all(y < chart.GY + chart.TOP_BAND for y in rows)


def test_line_with_missing_data_keeps_color_and_does_not_crash():
    values = list(_values(700, 96))
    values[48] = None
    png = chart.render_panel(_panel(lines={"awg": tuple(values)}))
    assert chart.TRANSPORT_COLORS["awg"] in _colors(png)


def test_empty_lines_and_empty_labels_render_grid_only():
    png = chart.render_panel(_panel(x_labels=(), lines={}))
    assert _open(png).size == (chart.WIDTH, chart.HEIGHT)
    assert chart.GRID in _colors(png)


def test_single_point_between_missing_values_is_drawn():
    values = [None] * 96
    values[40] = 800
    png = chart.render_panel(_panel(lines={"awg": tuple(values)}))
    assert chart.TRANSPORT_COLORS["awg"] in _colors(png)


def test_unknown_transport_gets_fallback_color():
    png = chart.render_panel(_panel(lines={"socks": _values(500, 96)}))
    assert chart.FALLBACK_COLORS[0] in _colors(png)


def test_down_level_badge_is_red():
    png = chart.render_panel(
        _panel(
            status="не работает",
            level=vpn_link.LEVEL_DOWN,
            lines={"awg": _values(600, 96)},
        )
    )
    assert chart.LEVEL_COLOR[vpn_link.LEVEL_DOWN] in _colors(png)


def test_unknown_level_does_not_crash():
    png = chart.render_panel(_panel(level="maybe", lines={"awg": _values(600, 96)}))
    assert _open(png).size == (chart.WIDTH, chart.HEIGHT)
    assert chart.UNKNOWN_LEVEL_COLOR in _colors(png)


def test_band_does_not_crash():
    png = chart.render_panel(
        _panel(
            lines={"awg": _values(600, 96)},
            bands=(chart.ChartBand(10, 20, "сервер"),),
        )
    )
    assert _open(png).size == (chart.WIDTH, chart.HEIGHT)
    assert chart.TRANSPORT_COLORS["awg"] in _colors(png)
