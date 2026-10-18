from __future__ import annotations

import json
import logging
import urllib.parse
from datetime import timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sa_home_bot.bot.tools.core import ToolContext, ToolSpec
from sa_home_bot.bot.tools.get_time import place_now
from sa_home_bot.bot.tools.translit import CYRILLIC_TO_LATIN
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.http import JSON_FETCH_ERRORS, get_json_async
from sa_home_bot.utils.timefmt import zone_or_none

log = logging.getLogger(__name__)


_HTTP_TIMEOUT_S = 10.0


_GEOCODE_CACHE: dict[str, tuple[float, float, str]] = {}


_FEATURE_RANK = {"PPLC": 4, "PPLA": 3, "PPLA2": 2, "PPLA3": 1}


def _geocode_score(item: dict[str, Any], names: set[str], country: str) -> tuple:
    own = str(item.get("name", "")).lower()
    alt = {own, own.translate(CYRILLIC_TO_LATIN)}
    country_ok = not country or country in str(item.get("country", "")).lower()
    return (
        country_ok,
        bool(alt & names),
        str(item.get("feature_code", "")).startswith("PPL"),
        _FEATURE_RANK.get(str(item.get("feature_code")), 0),
        item.get("population") or 0,
    )


async def _resolve_city(city: str) -> tuple[float, float, str] | None:
    key = city.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    raw_name, _, country = city.strip().partition(",")
    name, country = raw_name.strip().lower(), country.strip().lower()
    written = raw_name.strip()
    queries = list(dict.fromkeys([written, written.lower().translate(CYRILLIC_TO_LATIN)]))
    candidates: list[dict[str, Any]] = []
    for query in queries:
        url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={urllib.parse.quote(query)}&count=10&language=ru&format=json"
        )
        try:
            data = await get_json_async(url, _HTTP_TIMEOUT_S)
        except JSON_FETCH_ERRORS as exc:
            log.warning("tool_get_weather: геокодирование «%s» не удалось: %s", query, exc)
            continue
        candidates.extend(data.get("results") or [])
    if not candidates:
        return None
    names = {name, name.translate(CYRILLIC_TO_LATIN)}
    top = max(candidates, key=lambda item: _geocode_score(item, names, country))
    label = top.get("name", city)
    if top.get("country"):
        label = f"{label}, {top['country']}"
    resolved = (top["latitude"], top["longitude"], label)
    _GEOCODE_CACHE[key] = resolved
    return resolved


_WMO_RU = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман с изморозью",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снег",
    77: "снежная крупа",
    80: "небольшой ливень",
    81: "ливень",
    82: "сильный ливень",
    85: "снегопад",
    86: "сильный снегопад",
    95: "гроза",
    96: "гроза с градом",
    99: "сильная гроза с градом",
}


def _local_zone(data: dict[str, Any]) -> ZoneInfo | timezone:
    name = data.get("timezone")
    zone = zone_or_none(name) if isinstance(name, str) else None
    if zone is not None:
        return zone
    return timezone(timedelta(seconds=int(data.get("utc_offset_seconds") or 0)))


async def tool_get_weather(ctx: ToolContext, args: dict[str, Any]) -> str:
    requested_city = args.get("city")
    city = requested_city.strip() if isinstance(requested_city, str) else ""
    if not city:
        city = ctx.settings.weather.city
        if not city:
            return "погода не настроена — не задан ни город в вопросе, ни город дома в конфиге"
    resolved = await _resolve_city(city)
    if resolved is None:
        return f"не удалось определить координаты города «{city}»"
    lat, lon, label = resolved
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,"
        "wind_gusts_10m,relative_humidity_2m,cloud_cover,precipitation"
        "&daily=temperature_2m_min,temperature_2m_max,precipitation_probability_max"
        "&forecast_days=1&wind_speed_unit=ms&timezone=auto"
    )
    try:
        data = await get_json_async(url, _HTTP_TIMEOUT_S)
    except JSON_FETCH_ERRORS as exc:
        log.warning("tool_get_weather: %s", exc)
        return "не удалось получить погоду — сервис недоступен, повтори позже"
    current = data.get("current", {})
    daily = data.get("daily", {})

    def today(key: str) -> Any:
        values = daily.get(key)
        return values[0] if isinstance(values, list) and values else None

    code = current.get("weather_code")
    result: dict[str, Any] = {
        "location": label,
        "weather": _WMO_RU.get(code) if isinstance(code, int) else None,
        "temperature_c": current.get("temperature_2m"),
        "feels_like_c": current.get("apparent_temperature"),
        "today_min_c": today("temperature_2m_min"),
        "today_max_c": today("temperature_2m_max"),
        "precipitation_chance_pct": today("precipitation_probability_max"),
        "precipitation_mm": current.get("precipitation"),
        "cloud_cover_pct": current.get("cloud_cover"),
        "humidity_pct": current.get("relative_humidity_2m"),
        "wind_ms": current.get("wind_speed_10m"),
        "wind_gusts_ms": current.get("wind_gusts_10m"),
        "weather_code": code,
    }
    result.update(place_now(lat, lon, _local_zone(data), utc_now()))
    return json.dumps(result, ensure_ascii=False)


_DECL_WEATHER: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Что сейчас в любом городе мира (не только дома): погода словами, "
            "температура (сейчас, ощущается, мин/макс за день), осадки, облачность, "
            "ветер в м/с, а также местное время, день недели, время суток (ночь/"
            "рассвет/день/закат), высота солнца, восход и закат, фаза луны. Не "
            "считай время суток, восход или луну сам — бери из ответа. Если "
            "пользователь называет город, "
            "передай его в city; если спрашивает просто 'какая погода' без "
            "уточнения — не передавай city вовсе, вернётся погода дома."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": (
                        "Город, если он назван явно (например: Алматы); при "
                        "неоднозначности — со страной через запятую: «Бран, Румыния»"
                    ),
                }
            },
        },
    },
}


GET_WEATHER = ToolSpec(name="get_weather", handler=tool_get_weather, declaration=_DECL_WEATHER)
