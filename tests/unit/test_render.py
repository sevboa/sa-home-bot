from datetime import timedelta

from sa_home_bot.domain.models import (
    ALERTING,
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    KIND_CPU,
    KIND_DISK,
    OK,
    Event,
    HealthState,
)
from sa_home_bot.domain.render import (
    render_event,
    render_state_line,
    render_stats,
    render_status_full,
    render_status_summary,
)

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


def _states():
    return [
        HealthState("cpu:pkg", KIND_CPU, "Package", OK, 42.0, 0, None),
        HealthState("disk:/dev/sda", KIND_DISK, "sda", OK, 31.0, 0, None),
        HealthState("disk:/dev/sdb", KIND_DISK, "sdb", ALERTING, 58.0, 3, None),
    ]


def test_render_status_full_lists_all_components():
    text = render_status_full(_states())
    assert "перегрев" in text.lower()  # sdb в ALERTING
    assert text.count("\n") >= 3  # заголовок + строки компонентов


def test_render_status_full_empty():
    assert "нет данных" in render_status_full([]).lower()


def test_render_status_summary_has_uptime_temps_and_outage():
    from sa_home_bot.domain.models import POWER_UNEXPECTED, PowerEvent

    outage = PowerEvent(
        kind=POWER_UNEXPECTED,
        boot_at=BASE_TIME,
        down_at=BASE_TIME,
        up_at=BASE_TIME + timedelta(hours=2),
        down_approx=True,
    )
    text = render_status_summary(BASE_TIME, timedelta(hours=1, minutes=47), _states(), outage)
    assert "Сводка" in text
    assert "Аптайм" in text
    assert "CPU: 42.0°C" in text
    assert "sda" in text and "sdb" in text
    assert "🔥" in text  # sdb горячий
    assert "Последнее отключение" in text


def test_render_status_summary_no_data():
    text = render_status_summary(BASE_TIME, None, [], None)
    assert "нет данных" in text.lower()
    assert "Аптайм" not in text  # uptime None — строку не добавляем


def test_render_stats_counts_and_runs():
    counts = {"ok": 5, "error": 1}
    runs = [{"job_type": "sensor_scan", "status": "ok", "started_at": "2026-06-22T12:00:00"}]
    text = render_stats(counts, runs)
    assert "Всего прогонов: 6" in text
    assert "sensor_scan" in text


def test_render_stats_empty():
    assert "не было" in render_stats({}, [])
