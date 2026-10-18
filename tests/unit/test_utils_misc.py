from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.ids import random_seed32, short_token
from sa_home_bot.utils.lifespan import cancel_task
from sa_home_bot.utils.pending import PendingStore
from sa_home_bot.utils.plural import plural_ru
from sa_home_bot.utils.requirements import looks_like_sudo_denied, looks_like_sudo_message
from sa_home_bot.utils.text import clip, stem
from sa_home_bot.utils.timefmt import (
    ceil_div,
    format_dhms,
    format_local,
    split_dhms,
    zone_or_none,
)


def test_utc_now_is_aware_utc():
    assert utc_now().tzinfo is UTC


def test_ids():
    assert len(short_token()) == 8 and len(short_token(12)) == 12
    assert short_token() != short_token()
    assert 0 <= random_seed32() < 2**32


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, "5"), (1, "1"), (2, "2"), (4, "2"), (5, "5"), (11, "5"), (12, "5"), (14, "5"),
     (21, "1"), (22, "2"), (101, "1"), (111, "5"), (112, "5"), (1000, "5")],
)
def test_plural_ru(count, expected):
    assert plural_ru(count, "1", "2", "5") == expected


@pytest.mark.parametrize(
    ("seconds", "parts"),
    [(0, (0, 0, 0, 0)), (-5, (0, 0, 0, 0)), (59.9, (0, 0, 0, 59)), (93784, (1, 2, 3, 4))],
)
def test_split_dhms(seconds, parts):
    assert split_dhms(seconds) == parts


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0с"), (540, "9м"), (30360, "8ч 26м"), (93784, "1д 2ч 3м 4с"), (86400, "1д")],
)
def test_format_dhms_default_and_latin_suffixes(seconds, text):
    assert format_dhms(seconds) == text
    latin = format_dhms(seconds, days="d", hours="h", minutes="m", secs="s")
    assert latin == text.translate(str.maketrans("дчмс", "dhms"))


def test_ceil_div():
    assert [ceil_div(n, 60) for n in (0, 1, 60, 61, 130)] == [0, 1, 1, 2, 3]


def test_format_local_from_datetime_iso_and_tolerant():
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    expected = moment.astimezone().strftime("%d.%m %H:%M")
    assert format_local(moment, "%d.%m %H:%M") == expected
    assert format_local(moment.isoformat(), "%d.%m %H:%M") == expected
    assert format_local("мусор", "%H", tolerant=True) == "мусор"
    with pytest.raises(ValueError):
        format_local("мусор", "%H")


def test_zone_or_none():
    assert zone_or_none("Europe/Bucharest").key == "Europe/Bucharest"
    assert zone_or_none("Nowhere/City") is None
    assert zone_or_none("") is None
    assert zone_or_none(None) is None


def test_clip_variants():
    assert clip("short", 10) == "short"
    assert clip("  a   b  c ", 100, collapse_spaces=True) == "a b c"
    assert clip("abcdefghij", 5, mark="…") == "abcde…"
    assert clip("abcde fghij", 9, mark="…") == "abcde fgh…"
    assert clip("abcde fghij", 9, mark="…", at_word=True) == "abcde…"
    assert clip("abcdefghij kl", 8, mark="…", at_word=True) == "abcdefgh…"


@pytest.mark.parametrize(
    ("word", "expected"),
    [("кот", "кот"), ("озвучка", "озвуч"), ("глаза", "глаз"), ("медведя", "медве")],
)
def test_stem(word, expected):
    assert stem(word) == expected


def test_pending_store_evicts_oldest_and_pops_once():
    store: PendingStore[str] = PendingStore(2, iter("abc").__next__)
    first, second, third = store.add("1"), store.add("2"), store.add("3")
    assert (first, second, third) == ("a", "b", "c")
    assert store.get(first) is None and len(store) == 2
    assert store.get(second) == "2"
    assert store.pop(second) == "2" and store.pop(second) is None


@pytest.mark.parametrize(
    ("stderr", "denied", "message"),
    [
        ("sudo: a password is required", True, True),
        ("sudo: требуется пароль", False, True),
        ("Permission denied", True, True),
        ("a password is required", True, False),
        ("command not found", False, False),
    ],
)
def test_sudo_detectors(stderr, denied, message):
    assert looks_like_sudo_denied(stderr) is denied
    assert looks_like_sudo_message(stderr) is message


async def test_cancel_task():
    await cancel_task(None)
    task = asyncio.create_task(asyncio.sleep(10))
    await cancel_task(task)
    assert task.cancelled()
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    await cancel_task(done)
