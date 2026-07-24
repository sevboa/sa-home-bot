"""Инструменты tool-calling для /ai (LLM_INTEGRATION_PLAN.md §7-8): calc,
get_weather, convert_currency, remind. Диспетчер цикла (bot/ai_flow.py)
тестируется отдельно в test_ai_flow.py — здесь только сами обработчики в
изоляции."""

from __future__ import annotations

import math
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
def _clear_module_caches():
    # _GEOCODE_CACHE/_RATES_CACHE — на уровне модуля (кэш на время жизни
    # процесса, см. bot/tools.py), между тестами общий процесс — без сброса
    # второй тест с тем же городом/валютой получил бы результат первого.
    tools._GEOCODE_CACHE.clear()
    tools._RATES_CACHE.clear()
    yield
    tools._GEOCODE_CACHE.clear()
    tools._RATES_CACHE.clear()


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


async def test_calc_pi_constant(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "2 * pi"}) == "6.283185"


async def test_calc_e_constant(store):
    assert await tools.tool_calc(_ctx(store), {"expression": "e"}) == "2.718282"


async def test_calc_rounds_long_float_results(store):
    result = await tools.tool_calc(_ctx(store), {"expression": "1 / 3"})
    assert result == "0.333333"


async def test_calc_caret_as_power(store):
    # Живой баг 2026-07-24: модель пишет "1.5^2" (математическая нотация),
    # не питоновское "1.5**2" — тул должен понимать оба.
    assert await tools.tool_calc(_ctx(store), {"expression": "1.5^2"}) == "2.25"
    assert await tools.tool_calc(
        _ctx(store), {"expression": "2 * pi * 1.5^2 + 2 * pi * 1.5 * 2"}
    ) == str(round(2 * math.pi * 1.5**2 + 2 * math.pi * 1.5 * 2, 6))


async def test_calc_cylinder_surface_area_formula(store):
    # Живой баг 2026-07-24: модель раньше не могла посчитать формулу с π
    # через calc вообще (переменные были запрещены) — площадь поверхности
    # цилиндра (r=1.5, h=2): 2*pi*r*(r+h) = 2*pi*1.5*3.5 ≈ 32.9867.
    result = await tools.tool_calc(_ctx(store), {"expression": "2 * pi * 1.5 * (1.5 + 2)"})
    assert result.startswith("32.98")


async def test_calc_rejects_arbitrary_names(store):
    result = await tools.tool_calc(_ctx(store), {"expression": "x + 1"})
    assert result.startswith("ошибка")


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


async def test_get_weather_explicit_city_in_args_overrides_home(store, monkeypatch):
    # Живой баг 2026-07-24: тул раньше не принимал город аргументом вообще —
    # на "погода в Алматы" модель честно отвечала "умею только дома".
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            assert "name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C" in url
            return _GEOCODE_RESPONSE
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Москва"))  # дом — другой город
    result = await tools.tool_get_weather(_ctx(store, settings), {"city": "Казань"})
    assert "Казань, Россия" in result


async def test_get_weather_falls_back_to_home_city_without_args(store, monkeypatch):
    def fake_get_json(url, timeout):
        if "geocoding-api" in url:
            assert "name=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0" in url  # "Москва"
            return {
                "results": [
                    {"name": "Москва", "latitude": 55.75, "longitude": 37.6, "country": "Россия"}
                ]
            }
        return _FORECAST_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Москва"))
    result = await tools.tool_get_weather(_ctx(store, settings), {})
    assert "Москва, Россия" in result


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


# --- convert_currency ---

_RATES_RESPONSE = {"result": "success", "base_code": "USD", "rates": {"USD": 1, "RUB": 78.42}}


async def test_convert_currency_computes_result(store, monkeypatch):
    def fake_get_json(url, timeout):
        assert "open.er-api.com/v6/latest/USD" in url
        return _RATES_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": 100, "from": "usd", "to": "rub"}
    )
    assert '"rate": 78.42' in result
    assert '"result": 7842.0' in result


async def test_convert_currency_caches_rates_across_calls(store, monkeypatch):
    fetch_calls = 0

    def fake_get_json(url, timeout):
        nonlocal fetch_calls
        fetch_calls += 1
        return _RATES_RESPONSE

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    ctx = _ctx(store)
    await tools.tool_convert_currency(ctx, {"amount": 1, "from": "USD", "to": "RUB"})
    await tools.tool_convert_currency(ctx, {"amount": 2, "from": "USD", "to": "RUB"})
    assert fetch_calls == 1  # второй раз — из _RATES_CACHE, без сети


async def test_convert_currency_unknown_target_code(store, monkeypatch):
    monkeypatch.setattr(tools, "_get_json_sync", lambda url, timeout: _RATES_RESPONSE)
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": 10, "from": "USD", "to": "XXX"}
    )
    assert "неизвестный код валюты" in result


async def test_convert_currency_handles_network_error(store, monkeypatch):
    def fake_get_json(url, timeout):
        raise OSError("boom")

    monkeypatch.setattr(tools, "_get_json_sync", fake_get_json)
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": 10, "from": "USD", "to": "RUB"}
    )
    assert "недоступен" in result


async def test_convert_currency_rejects_non_numeric_amount(store):
    result = await tools.tool_convert_currency(
        _ctx(store), {"amount": "много", "from": "USD", "to": "RUB"}
    )
    assert result.startswith("ошибка")


async def test_convert_currency_rejects_missing_currency_codes(store):
    result = await tools.tool_convert_currency(_ctx(store), {"amount": 10, "from": "USD"})
    assert result.startswith("ошибка")


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
