"""Парсинг журнала загрузок (`last` + `journalctl`) и рендер /downtime."""

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

# Реальный по форме вывод `last -xR --time-format iso reboot` (новые сверху,
# непрерывная цепочка: up_at каждого = START следующей строки).
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


# journalctl --list-boots -o json: сессии по времени, first/last в микросекундах.
# first_entry намеренно на ~12 с позже last-START (как на реальной машине).
JOURNAL = json.dumps(
    [
        {
            "index": -1,
            "first_entry": _epoch_us("2026-07-04T12:15:51+0500"),
            "last_entry": _epoch_us("2026-07-04T15:12:01+0500"),  # обрыв сессии 12:15
        },
        {
            "index": -2,
            "first_entry": _epoch_us("2026-07-04T10:43:04+0500"),
            "last_entry": _epoch_us("2026-07-04T11:58:01+0500"),  # обрыв сессии 10:42
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
        POWER_UNEXPECTED,  # 04.07 12:15 crash
        POWER_UNEXPECTED,  # 04.07 10:42 crash
        POWER_UNEXPECTED,  # 29.06 10:12 crash
        POWER_CLEAN,       # 28.06 16:30 → 29.06 01:45
    ]


def test_clean_event_has_exact_down_and_up():
    events = parse_last_reboots(SAMPLE)
    clean = events[3]
    assert clean.kind == POWER_CLEAN
    assert clean.down_approx is False
    assert clean.down_at.hour == 1 and clean.down_at.minute == 45  # штатный shutdown
    assert clean.up_at.hour == 10 and clean.up_at.minute == 12     # поднялась 29.06 10:12
    assert clean.downtime is not None  # оба конца известны


def test_unexpected_without_journal_has_no_down_at():
    events = parse_last_reboots(SAMPLE)
    first = events[0]
    assert first.kind == POWER_UNEXPECTED
    assert first.down_at is None       # без journal обрыв неизвестен
    assert first.down_approx is True
    assert first.up_at.hour == 0 and first.up_at.minute == 23  # поднялась 05.07 00:23
    assert first.downtime is None


def test_enrich_fills_down_at_from_journal():
    events = parse_last_reboots(SAMPLE)
    boots = parse_journal_boots(JOURNAL)
    enriched = enrich_unexpected(events, boots)
    crash = enriched[0]  # сессия 04.07 12:15 → last_entry 15:12
    assert crash.kind == POWER_UNEXPECTED
    # Сравнение aware-datetime TZ-независимо (журнал хранит время в UTC).
    assert crash.down_at == datetime.fromisoformat("2026-07-04T15:12:01+0500")
    assert crash.down_approx is True
    # простой: 15:12 04.07 → 00:23 05.07 ≈ 9 ч 11 м
    assert crash.downtime is not None
    assert 9 * 3600 < crash.downtime.total_seconds() < 10 * 3600


def test_enrich_leaves_clean_untouched():
    events = parse_last_reboots(SAMPLE)
    boots = parse_journal_boots(JOURNAL)
    enriched = enrich_unexpected(events, boots)
    clean = enriched[3]
    assert clean.down_at.hour == 1 and clean.down_at.minute == 45  # не тронут journal-ом


def test_enrich_no_match_keeps_none():
    # journal без подходящих сессий → down_at остаётся None.
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
    assert parse_journal_boots('[{"index": 0}]') == []  # нет first/last


def test_render_shows_span_and_downtime():
    events = enrich_unexpected(parse_last_reboots(SAMPLE), parse_journal_boots(JOURNAL))
    text = render_downtime(events)
    assert "⚡ " in text
    assert "🔌 " in text
    assert " - " in text  # компактный диапазон вместо «→»
    assert "m" in text or "h" in text  # длительность в d/h/m/s


def test_render_downtime_empty():
    assert "Нет данных" in render_downtime([])


def test_render_downtime_empty_next_page():
    # Пустая следующая страница — не «нет данных вообще», а «дальше пусто».
    text = render_downtime([], offset=10)
    assert "Дальше отключений нет" in text
    assert "Нет данных" not in text


def test_render_downtime_page_header_shows_range():
    events = parse_last_reboots(SAMPLE, limit=2)
    text = render_downtime(events, offset=2)
    assert "Отключения 3–4" in text
    assert "Последние отключения" not in text


def test_render_downtime_entries_are_single_lines_without_numbering():
    # Каждая запись — одна компактная строка без номера и лишних переносов.
    events = parse_last_reboots(SAMPLE, limit=2)
    text = render_downtime(events, offset=2)
    lines = [line for line in text.split("\n") if line]
    assert any(line.startswith("⚡ ") for line in lines)
    assert not any(line[0].isdigit() for line in lines)  # без нумерации
    # Заголовок + 2 записи (пустая строка-разделитель отфильтрована) — 3 строки.
    assert len(lines) == 3


def test_read_power_events_sync_paginates(monkeypatch):
    # SAMPLE содержит 4 отключения — страница по 2 с offset=0 должна сигналить
    # о наличии следующей страницы, offset=2 (последние 2) — уже нет.
    def fake_run(args, requirement=None):
        if args[0] == "last":
            return SAMPLE
        return None  # journalctl недоступен — не важно для пагинации

    monkeypatch.setattr(power_module, "_run", fake_run)

    page1, has_next1 = read_power_events_sync(offset=0, limit=2)
    assert len(page1) == 2
    assert has_next1 is True

    page2, has_next2 = read_power_events_sync(offset=2, limit=2)
    assert len(page2) == 2
    assert has_next2 is False

    page3, has_next3 = read_power_events_sync(offset=4, limit=2)
    assert page3 == []
    assert has_next3 is False


def test_fmt_duration_units():
    from datetime import timedelta

    from sa_home_bot.domain.render import _fmt_duration

    assert _fmt_duration(timedelta(seconds=30)) == "30s"
    assert _fmt_duration(timedelta(minutes=9)) == "9m"
    assert _fmt_duration(timedelta(hours=8, minutes=26, seconds=15)) == "8h 26m 15s"
    assert _fmt_duration(timedelta(days=1, hours=2)) == "1d 2h"


def test_fmt_uptime_short_sub_minute_is_html_safe():
    # Аптайм <1 минуты не должен давать сырой «<» — иначе Telegram HTML падает.
    from datetime import timedelta

    from sa_home_bot.domain.render import _fmt_uptime_short

    assert _fmt_uptime_short(timedelta(seconds=30)) == "&lt;1 m"


def test_read_power_events_unsupported_on_windows(monkeypatch):
    import sys

    called = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(power_module, "_run", lambda *a, **kw: called.append(a))
    events, has_next = power_module.read_power_events_sync()
    assert (events, has_next) == ([], False)
    assert called == []  # ни last, ни journalctl не дёргались
