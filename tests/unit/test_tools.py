"""Инструменты tool-calling для /ai (LLM_INTEGRATION_PLAN.md §7-8): calc,
get_weather, remind. Диспетчер цикла (bot/ai_flow.py) тестируется отдельно
в test_ai_flow.py — здесь только сами обработчики в изоляции."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from sa_home_bot.bot import tools
from sa_home_bot.config import Settings, WeatherConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

CHAT_ID = 111


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


@pytest.fixture(autouse=True)
def _clear_geocode_cache():
    # _GEOCODE_CACHE — на уровне модуля (кэш на время жизни процесса, см.
    # bot/tools.py), между тестами общий процесс — без сброса второй тест с
    # тем же названием города получил бы результат геокодинга первого.
    tools._GEOCODE_CACHE.clear()
    yield
    tools._GEOCODE_CACHE.clear()


def _ctx(store, settings=None, chat_id=CHAT_ID):
    return tools.ToolContext(chat_id=chat_id, store=store, settings=settings or Settings())


# --- calc ---


async def test_calc_basic_arithmetic(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "2 + 2"}) == "4"


async def test_calc_operator_precedence_and_parens(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "(2 + 3) * 4"}) == "20"


async def test_calc_division_returns_float(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "7 / 2"}) == "3.5"


async def test_calc_power(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "2 ** 10"}) == "1024"


async def test_calc_rejects_non_arithmetic_expression(store):
    result = await tools.tool_calc(
        _ctx(store), {"expression": "__import__('os').system('ls')"}
    )
    assert result.startswith("ошибка")


async def test_calc_rejects_empty_expression(store):
    assert await tools.tool_calc(_ctx(store), {"expression": ""}) == "ошибка: пустое выражение"


async def test_calc_division_by_zero(store):
    result = await tools.tool_calc(_ctx(store), {"expression": "1 / 0"})
    assert result.startswith("ошибка")


async def test_calc_rejects_missing_expression(store):
    result = await tools.tool_calc(_ctx(store), {})
    assert result.startswith("ошибка")


# --- get_weather ---


_GEOCODE_RESPONSE = {
    "results": [{"name": "Казань", "latitude": 55.79, "longitude": 49.12, "country": "Россия"}]
}
_FORECAST_RESPONSE = {
    "current": {
        "temperature_2m": 20.5,
        "apparent_temperature": 19.0,
        "wind_speed_10m": 3.0,
        "weather_code": 1,
    }
}


async def test_get_weather_not_configured_by_default(store):
    result = await tools.tool_get_weather(_ctx(store), {})
    assert "не настроена" in result


async def test_get_weather_returns_current_conditions(store, monkeypatch):
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            assert "name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C" in url  # "Казань" URL-encoded
            return _GEOCODE_RESPONSE
        assert "latitude=55.79" in url and "longitude=49.12" in url
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert '"temperature_c": 20.5' in result
    assert "Казань, Россия" in result


async def test_get_weather_caches_geocoding_across_calls(store, monkeypatch):
    geocode_calls = 0

    def fake_get_json(url, timeout):
        nonlocal geocode_calls
        if "geocoding-api" in url:
            geocode_calls += 1
            return _GEOCODE_RESPONSE
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    ctx = _ctx(store, settings)
    await tools.tool_get_weather(ctx, {})
    await tools.tool_get_weather(ctx, {})
    assert geocode_calls == 1  # второй раз — из _GEOCODE_CACHE, без сети


async def test_get_weather_city_not_found(store, monkeypatch):
    def fake_get_json(url, timeout):
        return {"results": []}

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Несуществующгород"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "не удалось определить координаты" in result


async def test_get_weather_handles_geocoding_network_error(store, monkeypatch):
    def fake_get_json(url, timeout):
        raise OSError("boom")

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "не удалось определить координаты" in result


async def test_get_weather_handles_forecast_network_error(store, monkeypatch):
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            return _GEOCODE_RESPONSE
        raise OSError("boom")

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "недоступен" in result


# --- remind ---


async def test_remind_creates_reminder_for_future_time(store):
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store), {"when": when, "text": "полить цветы"})
    assert "создано" in result
    due = await store.due_reminders(datetime.now(UTC) + timedelta(hours=2))
    assert [r["text"] for r in due] == ["полить цветы"]


async def test_remind_rejects_past_time(store):
    when = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store), {"when": when, "text": "поздно"})
    assert result.startswith("ошибка")


async def test_remind_rejects_invalid_iso(store):
    result = await tools.tool_remind(_ctx(store), {"when": "завтра", "text": "текст"})
    assert result.startswith("ошибка")


async def test_remind_rejects_missing_text(store):
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(store), {"when": when, "text": ""})
    assert result.startswith("ошибка")


async def test_remind_without_chat_id(store):
    result = await tools.tool_remind(_ctx(store, chat_id=None), {"when": "x", "text": "x"})
    assert "вне чата" in result
