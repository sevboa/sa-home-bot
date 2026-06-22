from sentinel_bot.domain.models import (
    ALERTING,
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    OK,
    Event,
    HealthState,
)
from sentinel_bot.domain.render import render_event, render_state_line

from .conftest import BASE_TIME


def test_render_overheat_started():
    event = Event(EVENT_OVERHEAT_STARTED, "cpu:pkg", "cpu", "Package", 92.5, BASE_TIME)
    text = render_event(event)
    assert "Перегрев" in text
    assert "92.5°C" in text
    assert "CPU" in text


def test_render_overheat_cleared():
    event = Event(EVENT_OVERHEAT_CLEARED, "disk:/dev/sda", "disk", "SSD", 50.0, BASE_TIME)
    text = render_event(event)
    assert "Норма" in text
    assert "остыл" in text
    assert "Диск" in text


def test_render_escapes_html_in_label():
    event = Event(EVENT_OVERHEAT_STARTED, "cpu:x", "cpu", "<b>&evil</b>", 90.0, BASE_TIME)
    text = render_event(event)
    assert "<b>&evil" not in text
    assert "&lt;b&gt;" in text


def test_render_state_line_alerting_and_ok():
    hot = HealthState("cpu:pkg", "cpu", "Package", ALERTING, 91.0, 0, BASE_TIME)
    cool = HealthState("cpu:pkg", "cpu", "Package", OK, 45.0, 0, None)
    assert "🔥" in render_state_line(hot)
    assert "✅" in render_state_line(cool)
