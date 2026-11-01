from __future__ import annotations

import json
from datetime import datetime

from sa_home_bot.domain.models import POWER_CLEAN, POWER_UNEXPECTED
from sa_home_bot.domain.render import render_downtime
from sa_home_bot.sensors import power as power_module
from sa_home_bot.sensors.power import (
    enrich_unexpected,
    parse_journal_boots,
    parse_last_reboots,
    read_power_events_sync,
)

SAMPLE = """\
reboot   system boot  6.12.90 2026-07-05T00:23:52+0500  - still running
reboot   system boot  6.12.90 2026-07-04T12:15:39+0500  - crash
reboot   system boot  6.12.90 2026-07-04T10:42:51+0500  - crash
reboot   system boot  6.12.90 2026-06-29T10:12:08+0500  - crash
reboot   system boot  6.12.90 2026-06-28T16:30:05+0500  - 2026-06-29T01:45:53+0500   (09:15)

wtmp begins Mon Jun 22 15:28:00 2026
"""


def _epoch_us(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1_000_000)


JOURNAL = json.dumps(
    [
        {
            "index": -1,
            "first_entry": _epoch_us("2026-07-04T12:15:51+0500"),
            "last_entry": _epoch_us("2026-07-04T15:12:01+0500"),
        },
        {
            "index": -2,
            "first_entry": _epoch_us("2026-07-04T10:43:04+0500"),
            "last_entry": _epoch_us("2026-07-04T11:58:01+0500"),
        },
        {
            "index": 0,
            "first_entry": _epoch_us("2026-07-05T00:24:02+0500"),
            "last_entry": _epoch_us("2026-07-05T01:26:01+0500"),
        },
    ]
)


def test_parse_classifies_crash_and_clean():
    events = parse_last_reboots(SAMPLE)
    assert [e.kind for e in events] == [
        POWER_UNEXPECTED,
        POWER_UNEXPECTED,
        POWER_UNEXPECTED,
        POWER_CLEAN,
    ]


def test_clean_event_has_exact_down_and_up():
    events = parse_last_reboots(SAMPLE)
    clean = events[3]
    assert clean.kind == POWER_CLEAN
    assert clean.down_approx is False
    assert clean.down_at.hour == 1 and clean.down_at.minute == 45
    assert clean.up_at.hour == 10 and clean.up_at.minute == 12
    assert clean.downtime is not None


def test_unexpected_without_journal_has_no_down_at():
    events = parse_last_reboots(SAMPLE)
    first = events[0]
    assert first.kind == POWER_UNEXPECTED
    assert first.down_at is None
    assert first.down_approx is True
    assert first.up_at.hour == 0 and first.up_at.minute == 23
    assert first.downtime is None


def test_enrich_fills_down_at_from_journal():
    events = parse_last_reboots(SAMPLE)
    boots = parse_journal_boots(JOURNAL)
    enriched = enrich_unexpected(events, boots)
    crash = enriched[0]
    assert crash.kind == POWER_UNEXPECTED
    assert crash.down_at == datetime.fromisoformat("2026-07-04T15:12:01+0500")
    assert crash.down_approx is True
    assert crash.downtime is not None
    assert 9 * 3600 < crash.downtime.total_seconds() < 10 * 3600


def test_enrich_leaves_clean_untouched():
    events = parse_last_reboots(SAMPLE)
    boots = parse_journal_boots(JOURNAL)
    enriched = enrich_unexpected(events, boots)
    clean = enriched[3]
    assert clean.down_at.hour == 1 and clean.down_at.minute == 45


def test_enrich_no_match_keeps_none():
    events = parse_last_reboots(SAMPLE)
    far = json.dumps(
        [{"index": 0, "first_entry": _epoch_us("2020-01-01T00:00:00+0500"),
          "last_entry": _epoch_us("2020-01-01T01:00:00+0500")}]
    )
    enriched = enrich_unexpected(events, parse_journal_boots(far))
    assert enriched[0].down_at is None


def test_still_running_is_not_an_event():
    assert len(parse_last_reboots(SAMPLE)) == 4


def test_limit_truncates_to_newest():
    events = parse_last_reboots(SAMPLE, limit=2)
    assert len(events) == 2
    assert all(e.kind == POWER_UNEXPECTED for e in events)


def test_parse_ignores_noise_lines():
    assert parse_last_reboots("") == []
    assert parse_last_reboots("wtmp begins ...\n\n") == []


def test_parse_journal_boots_handles_garbage():
    assert parse_journal_boots("not json") == []
    assert parse_journal_boots("[]") == []
    assert parse_journal_boots('[{"index": 0}]') == []


def test_render_shows_span_and_downtime():
    events = enrich_unexpected(parse_last_reboots(SAMPLE), parse_journal_boots(JOURNAL))
    text = render_downtime(events)
    assert "⚡ " in text
    assert "🔌 " in text
    assert " - " in text
    assert "m" in text or "h" in text


def test_render_downtime_empty():
    assert "Нет данных" in render_downtime([])


def test_render_downtime_empty_next_page():
    text = render_downtime([], offset=10)
    assert "Дальше отключений нет" in text
    assert "Нет данных" not in text


def test_render_downtime_page_header_shows_range():
    events = parse_last_reboots(SAMPLE, limit=2)
    text = render_downtime(events, offset=2)
    assert "Отключения 3–4" in text
    assert "Последние отключения" not in text


def test_render_downtime_entries_are_single_lines_without_numbering():
    events = parse_last_reboots(SAMPLE, limit=2)
    text = render_downtime(events, offset=2)
    lines = [line for line in text.split("\n") if line]
    assert any(line.startswith("⚡ ") for line in lines)
    assert not any(line[0].isdigit() for line in lines)
    assert len(lines) == 3


def test_read_power_events_sync_paginates(monkeypatch):
    def fake_run(args, requirement=None):
        if args[0] == "last":
            return SAMPLE
        return None

    monkeypatch.setattr(power_module, "_run", fake_run)
    monkeypatch.setattr(
        power_module,
        "LAST_REQUIREMENT",
        type("MockReq", (), {"available": lambda self: True})(),
    )

    page1, has_next1 = read_power_events_sync(offset=0, limit=2)
    assert len(page1) == 2
    assert has_next1 is True

    page2, has_next2 = read_power_events_sync(offset=2, limit=2)
    assert len(page2) == 2
    assert has_next2 is False

    page3, has_next3 = read_power_events_sync(offset=4, limit=2)
    assert page3 == []
    assert has_next3 is False


def test_format_dhms_en_units():
    from datetime import timedelta

    from sa_home_bot.domain.render import _format_dhms_en

    assert _format_dhms_en(timedelta(seconds=30)) == "30s"
    assert _format_dhms_en(timedelta(minutes=9)) == "9m"
    assert _format_dhms_en(timedelta(hours=8, minutes=26, seconds=15)) == "8h 26m 15s"
    assert _format_dhms_en(timedelta(days=1, hours=2)) == "1d 2h"


def test_format_largest_unit_en_sub_minute_is_html_safe():
    from datetime import timedelta

    from sa_home_bot.domain.render import _format_largest_unit_en

    assert _format_largest_unit_en(timedelta(seconds=30)) == "&lt;1 m"


def test_read_power_events_unsupported_on_windows(monkeypatch):
    import sys

    called = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(power_module, "_run", lambda *a, **kw: called.append(a))
    events, has_next = power_module.read_power_events_sync()
    assert (events, has_next) == ([], False)
    assert called == []
