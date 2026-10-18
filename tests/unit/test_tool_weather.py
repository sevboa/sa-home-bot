from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sa_home_bot.bot import tools
from sa_home_bot.bot.tools import weather
from sa_home_bot.config import (
    Settings,
    WeatherConfig,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)


@pytest.fixture(autouse=True)
def _clear_geocode_cache():
    weather._GEOCODE_CACHE.clear()
    yield
    weather._GEOCODE_CACHE.clear()


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


async def test_get_weather_not_configured_by_default():
    result = await tools.tool_get_weather(_ctx(), {})
    assert "не настроена" in result


async def test_get_weather_returns_current_conditions(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        if "geocoding-api" in url:
            assert "name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C" in url or "name=kazan" in url
            return _GEOCODE_RESPONSE
        assert "latitude=55.79" in url and "longitude=49.12" in url
        return _FORECAST_RESPONSE

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(None, settings), {})
    assert '"temperature_c": 20.5' in result
    assert "Казань, Россия" in result


async def test_get_weather_adds_local_time_sun_and_moon(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        if "geocoding-api" in url:
            return _GEOCODE_RESPONSE
        assert "timezone=auto" in url
        return {
            **_FORECAST_RESPONSE,
            "timezone": "Europe/Moscow",
            "utc_offset_seconds": 10800,
            "daily": {"temperature_2m_min": [11.0], "temperature_2m_max": [22.0]},
        }

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    data = json.loads(await tools.tool_get_weather(_ctx(None, settings), {}))
    assert data["weather"] == "преимущественно ясно"
    assert data["today_max_c"] == 22.0
    assert data["utc_offset"] == "+0300"
    assert data["time_of_day"] in ("ночь", "рассвет", "день", "закат")
    assert set(data["sun"]) >= {"altitude_deg", "sunrise", "sunset"}
    assert data["moon"]["phase"] and 0 <= data["moon"]["illumination_pct"] <= 100


async def test_resolve_city_prefers_exact_big_city_and_country(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        if "name=bran" in url:
            return {
                "results": [
                    {
                        "name": "Bran",
                        "latitude": 45.5,
                        "longitude": 25.3,
                        "country": "Румыния",
                        "feature_code": "PPLA2",
                        "population": 5177,
                    },
                ]
            }
        return {
            "results": [
                {
                    "name": "Бран",
                    "latitude": 45.2,
                    "longitude": 0.1,
                    "country": "Франция",
                    "feature_code": "PPL",
                    "population": 124,
                },
                {
                    "name": "Бранч",
                    "latitude": 35.0,
                    "longitude": -93.0,
                    "country": "США",
                    "feature_code": "PPL",
                    "population": 358,
                },
            ]
        }

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    assert (await weather._resolve_city("Бран"))[2] == "Bran, Румыния"
    assert (await weather._resolve_city("Бран, Франция"))[2] == "Бран, Франция"


def test_place_now_falls_back_to_offset_for_unknown_zone():
    zone = weather._local_zone({"timezone": "Nowhere/Nope", "utc_offset_seconds": 18000})
    now = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    data = tools.place_now(43.24, 76.95, zone, now)
    assert data["local_time"] == "12:00"
    assert data["time_of_day"] == "день"


async def test_get_weather_caches_geocoding_across_calls(monkeypatch):
    geocode_calls = 0

    def fake_get_json(url, timeout, headers=None):
        nonlocal geocode_calls
        if "geocoding-api" in url:
            geocode_calls += 1
            return _GEOCODE_RESPONSE
        return _FORECAST_RESPONSE

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    ctx = _ctx(None, settings)
    await tools.tool_get_weather(ctx, {})
    first = geocode_calls
    await tools.tool_get_weather(ctx, {})
    assert geocode_calls == first


async def test_get_weather_explicit_city_in_args_overrides_home(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        if "geocoding-api" in url:
            assert "name=%D0%9A%D0%B0%D0%B7%D0%B0%D0%BD%D1%8C" in url or "name=kazan" in url
            return _GEOCODE_RESPONSE
        return _FORECAST_RESPONSE

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Москва"))
    result = await tools.tool_get_weather(_ctx(None, settings), {"city": "Казань"})
    assert "Казань, Россия" in result


async def test_get_weather_falls_back_to_home_city_without_args(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        if "geocoding-api" in url:
            assert "name=%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0" in url or "name=moskva" in url
            return {
                "results": [
                    {"name": "Москва", "latitude": 55.75, "longitude": 37.6, "country": "Россия"}
                ]
            }
        return _FORECAST_RESPONSE

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Москва"))
    result = await tools.tool_get_weather(_ctx(None, settings), {})
    assert "Москва, Россия" in result


def _boom(url, timeout, headers=None):
    raise OSError("boom")


@pytest.mark.parametrize(
    "fake_get_json",
    [lambda url, timeout, headers=None: {"results": []}, _boom],
    ids=["city-not-found", "geocoding-network-error"],
)
async def test_get_weather_without_coordinates(monkeypatch, fake_get_json):
    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(None, settings), {})
    assert "не удалось определить координаты" in result


async def test_get_weather_handles_forecast_network_error(monkeypatch):
    def fake_get_json(url, timeout, headers=None):
        if "geocoding-api" in url:
            return _GEOCODE_RESPONSE
        raise OSError("boom")

    monkeypatch.setattr("sa_home_bot.utils.http.get_json", fake_get_json)
    settings = Settings(weather=WeatherConfig(city="Казань"))
    result = await tools.tool_get_weather(_ctx(None, settings), {})
    assert "недоступен" in result
