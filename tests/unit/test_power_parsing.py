"""Парсинг журнала загрузок (`last`) и рендер /downtime."""

from __future__ import annotations

from sa_home_bot.domain.models import POWER_CLEAN, POWER_UNEXPECTED
from sa_home_bot.domain.render import render_downtime
from sa_home_bot.sensors.power import parse_last_reboots

# Реальный по форме вывод `last -xR --time-format iso reboot` (новые сверху).
SAMPLE = """\
reboot   system boot  6.12.90 2026-07-05T00:23:52+0500  - still running
reboot   system boot  6.12.90 2026-07-04T12:15:39+0500  - crash
reboot   system boot  6.12.90 2026-07-04T10:42:51+0500  - crash
reboot   system boot  6.12.90 2026-06-28T16:30:05+0500  - 2026-06-29T01:45:53+0500   (09:15)
reboot   system boot  6.12.90 2026-06-28T04:24:37+0500  - 2026-06-28T10:00:00+0500   (05:35)

wtmp begins Mon Jun 22 15:28:00 2026
"""


def test_parse_classifies_crash_and_clean():
    events = parse_last_reboots(SAMPLE)
    kinds = [e.kind for e in events]
    assert kinds == [
        POWER_UNEXPECTED,  # 04.07 12:15 crash
        POWER_UNEXPECTED,  # 04.07 10:42 crash
        POWER_CLEAN,       # 28.06 16:30 → 29.06 01:45
        POWER_CLEAN,       # 28.06 04:24 → 28.06 10:00
    ]


def test_clean_event_has_shutdown_time():
    events = parse_last_reboots(SAMPLE)
    clean = events[2]
    assert clean.kind == POWER_CLEAN
    assert clean.shutdown_at is not None
    assert clean.shutdown_at.hour == 1 and clean.shutdown_at.minute == 45


def test_unexpected_event_recovery_time():
    events = parse_last_reboots(SAMPLE)
    # Самое новое отключение (crash 12:15) — машина поднялась в текущую сессию 00:23.
    first = events[0]
    assert first.kind == POWER_UNEXPECTED
    assert first.shutdown_at is None
    assert first.next_boot_at is not None
    assert first.next_boot_at.hour == 0 and first.next_boot_at.minute == 23
    # Следующий crash (10:42) поднялся в сессию 12:15.
    assert events[1].next_boot_at.hour == 12 and events[1].next_boot_at.minute == 15


def test_still_running_is_not_an_event():
    events = parse_last_reboots(SAMPLE)
    # 5 reboot-строк, но текущая (still running) — не отключение.
    assert len(events) == 4


def test_limit_truncates_to_newest():
    events = parse_last_reboots(SAMPLE, limit=2)
    assert len(events) == 2
    assert all(e.kind == POWER_UNEXPECTED for e in events)


def test_parse_ignores_noise_lines():
    assert parse_last_reboots("") == []
    assert parse_last_reboots("wtmp begins ...\n\n") == []


def test_render_downtime_shows_both_reasons():
    events = parse_last_reboots(SAMPLE)
    text = render_downtime(events)
    assert "⚡ внезапно" in text
    assert "🔌 штатно" in text
    assert "поднялась 05.07 00:23" in text
    assert "🔌 штатно — 29.06 01:45" in text


def test_render_downtime_empty():
    assert "Нет данных" in render_downtime([])
