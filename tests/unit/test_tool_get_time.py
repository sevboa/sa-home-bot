from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sa_home_bot.bot import tools
from sa_home_bot.bot.tools import get_time
from tests.unit.tools_support import make_ctx

MOSCOW_EVENING = "2026-07-24T20:28:00+03:00"


async def _get_time(args):
    return await tools.tool_get_time(make_ctx(None), args)


@pytest.mark.parametrize(
    ("place", "timezone", "utc_offset", "local_time", "weekday"),
    [
        ("Казахстан", "Asia/Almaty", "+05:00", "2026-07-24 22:28", "пятница"),
        ("  МОСКВА  ", "Europe/Moscow", "+03:00", "2026-07-24 20:28", "пятница"),
        ("Гринвич", "UTC", "+00:00", "2026-07-24 17:28", "пятница"),
        ("Италия", "Europe/Rome", "+02:00", "2026-07-24 19:28", "пятница"),
    ],
    ids=[
        "kazakhstan",
        "case-and-whitespace-insensitive",
        "greenwich-is-utc-not-london-dst",
        "italy",
    ],
)
async def test_get_time_single_place(place, timezone, utc_offset, local_time, weekday):
    data = json.loads(await _get_time({"place": place, "at": MOSCOW_EVENING}))
    assert data["timezone"] == timezone
    assert data["utc_offset"] == utc_offset
    assert data["local_time"] == local_time
    assert data["weekday"] == weekday


async def test_get_time_defaults_to_now_without_at(monkeypatch):
    monkeypatch.setattr(get_time, "utc_now", lambda: datetime(2026, 7, 24, 17, 28, tzinfo=UTC))
    data = json.loads(await _get_time({"place": "Москва"}))
    assert data["local_time"] == "2026-07-24 20:28"


async def test_get_time_unknown_place_is_honest_refusal():
    assert (await _get_time({"place": "Атлантида"})).startswith("не знаю часовой пояс")


async def test_get_time_places_returns_each_place_and_differences():
    data = json.loads(
        await _get_time({"places": ["Москва", "Италия", "Казахстан"], "at": MOSCOW_EVENING})
    )
    by_place = {p["place"]: p for p in data["places"]}
    assert by_place["Москва"]["utc_offset"] == "+03:00"
    assert by_place["Италия"]["utc_offset"] == "+02:00"
    assert by_place["Казахстан"]["utc_offset"] == "+05:00"
    assert any("Москва впереди Италия на 1 ч" in d for d in data["differences"])
    assert any("Казахстан впереди Москва на 2 ч" in d for d in data["differences"])


async def test_get_time_places_single_entry_behaves_like_place():
    assert await _get_time({"places": ["Москва"], "at": MOSCOW_EVENING}) == await _get_time(
        {"place": "Москва", "at": MOSCOW_EVENING}
    )


async def test_get_time_places_partial_unknown_still_answers_known():
    data = json.loads(await _get_time({"places": ["Москва", "Атлантида"], "at": MOSCOW_EVENING}))
    assert [p["place"] for p in data["places"]] == ["Москва"]
    assert data["unknown_places"] == ["Атлантида"]


async def test_get_time_places_all_unknown_is_honest_refusal():
    result = await _get_time({"places": ["Атлантида", "Нарния"]})
    assert result.startswith("не знаю часовой пояс")


async def test_get_time_places_same_offset_reports_no_difference():
    data = json.loads(await _get_time({"places": ["Казахстан", "Алматы"], "at": MOSCOW_EVENING}))
    assert "одинаковое время" in data["differences"][0]


@pytest.mark.parametrize(
    "args",
    [{}, {"place": "Москва", "at": "2026-07-24T20:28:00"}, {"place": "Москва", "at": "не дата"}],
    ids=["missing-place", "naive-at", "malformed-at"],
)
async def test_get_time_rejects(args):
    assert (await _get_time(args)).startswith("ошибка")
