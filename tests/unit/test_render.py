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


def _disks():
    from sa_home_bot.domain.models import DISK_OK, DISK_WARN, DiskSummary

    return [
        DiskSummary("HDD1", DISK_WARN, 31.0, 137_000_000_000, 245_000_000_000, "ST9250"),
        DiskSummary("HDD2", DISK_OK, 29.0, 277_000_000_000, 314_000_000_000, "Hitachi"),
        DiskSummary("eMMC", None, None, 49_000_000_000, 56_000_000_000, None),
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
    cpu = [s for s in _states() if s.kind == KIND_CPU]
    text = render_status_summary(
        BASE_TIME,
        timedelta(hours=1, minutes=47),
        cpu,
        _disks(),
        outage,
        cpu_warn_c=80.0,
        cpu_crit_c=90.0,
        disk_warn_c=55.0,
        disk_crit_c=65.0,
    )
    assert "2026-06-22" in text  # дата отчёта, без слова "Сводка"
    assert "uptime: 1 h" in text
    assert "CPU: 42.0°C" in text
    assert "⚠️ HDD ST9250" in text and "✅ HDD Hitachi" in text  # иконка + модель
    assert "❔ eMMC:" in text  # SMART недоступен, однострочно (нет температуры)
    assert "31°C" in text  # температура HDD1
    assert "108 из 245 ГБ" in text  # занятое место (было — свободное)
    assert "last off:" in text


def test_render_status_summary_no_data():
    text = render_status_summary(
        BASE_TIME, None, [], [], None,
        cpu_warn_c=80.0, cpu_crit_c=90.0, disk_warn_c=55.0, disk_crit_c=65.0,
    )
    assert "2026-06-22" in text
    assert "uptime:" not in text  # uptime None — строку не добавляем
    assert "HDD" not in text  # дисков нет — секцию не показываем


def test_disk_temp_mood_matches_configured_alert_thresholds():
    # «Настроение» температуры — та же шкала warn_c/crit_c, что и у реальных
    # алертов (не выдуманные отдельно числа): 48°C ниже warn_c=55 — норма,
    # но выше warn_c=45 — уже «жарко». Иконка не должна противоречить алерту.
    from sa_home_bot.domain.models import DISK_OK, DiskSummary
    from sa_home_bot.domain.render import render_disk_line

    disk = DiskSummary("HDD1", DISK_OK, 48.0, 100_000_000_000, 200_000_000_000, "X")
    assert "🙂" in render_disk_line(disk, warn_c=55.0, crit_c=65.0)
    assert "🥵" in render_disk_line(disk, warn_c=45.0, crit_c=65.0)


def test_render_stats_counts_and_runs():
    counts = {"ok": 5, "error": 1}
    runs = [{"job_type": "sensor_scan", "status": "ok", "started_at": "2026-06-22T12:00:00"}]
    text = render_stats(counts, runs)
    assert "Всего прогонов: 6" in text
    assert "sensor_scan" in text


def test_render_stats_empty():
    assert "не было" in render_stats({}, [])
