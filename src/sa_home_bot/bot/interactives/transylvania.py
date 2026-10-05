"""Время суток и погода за окном кабинета Альфреда (Этап 49.2).

Замок — в Трансильвании (точка — замок Бран), часы там реальные
(Europe/Bucharest). Светло или темно — по восходу и закату для даты, так что
световой день меняется с сезоном; погода — реальная, с Open-Meteo (без
ключа), кэш на час. Дождь, гроза, туман, снег днём дают «вечерний» свет.
Цель — устойчивая и загадочная обстановка, не метеосводка (решение
пользователя 2026-09-30): сбой погоды — просто без погоды.

Сеть — ``urllib`` + ``asyncio.to_thread``, как в net/searxng.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Bucharest")
LAT, LON = 45.515, 25.367  # замок Бран
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}&current=weather_code&timezone=Europe%2FBucharest"
)
WEATHER_TTL_S = 3600.0
WEATHER_TIMEOUT_S = 6.0
TWILIGHT = timedelta(minutes=45)

PHASE_DAWN = "dawn"
PHASE_DAY = "day"
PHASE_DUSK = "dusk"
PHASE_NIGHT = "night"

WEATHER_CLEAR = "clear"
WEATHER_CLOUDY = "cloudy"
WEATHER_FOG = "fog"
WEATHER_RAIN = "rain"
WEATHER_SNOW = "snow"
WEATHER_STORM = "storm"
_GLOOMY = {WEATHER_FOG, WEATHER_RAIN, WEATHER_SNOW, WEATHER_STORM}

# (по-русски — Альфреду и Ведущему, по-английски — в промпт снимка)
_PHASE_TEXT = {
    PHASE_DAWN: ("раннее утро, за окном светает", "cold dawn light through the window"),
    PHASE_DAY: ("день", "daylight through the window"),
    PHASE_DUSK: ("сумерки, за окном закат", "orange sunset light, long shadows"),
    PHASE_NIGHT: ("ночь, горят свечи", "night, dark night window, moonlight, candlelight"),
}
_WEATHER_TEXT = {
    WEATHER_CLEAR: ("ясно", ""),
    WEATHER_CLOUDY: ("пасмурно", "overcast sky"),
    WEATHER_FOG: ("туман за окном", "thick fog outside the window"),
    WEATHER_RAIN: ("дождь стучит в окно", "rain on the window"),
    WEATHER_SNOW: ("за окном снег", "snow falling outside the window"),
    WEATHER_STORM: ("гроза", "thunderstorm, lightning outside the window"),
}


def _weather_kind(code: int) -> str:
    """WMO weather code → наша категория."""
    if code in (0, 1):
        return WEATHER_CLEAR
    if code in (2, 3):
        return WEATHER_CLOUDY
    if code in (45, 48):
        return WEATHER_FOG
    if code in (71, 73, 75, 77, 85, 86):
        return WEATHER_SNOW
    if code >= 95:
        return WEATHER_STORM
    return WEATHER_RAIN  # морось, дождь, ливни, ледяной дождь


def sun_times(day: date) -> tuple[datetime, datetime]:
    """Восход и закат (UTC) над Браном — упрощённое уравнение NOAA, точность
    пара минут, нам хватает с запасом."""
    n = day.timetuple().tm_yday
    gamma = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    lat = math.radians(LAT)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl)) - math.tan(
        lat
    ) * math.tan(decl)
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    sunrise = midnight + timedelta(minutes=720 - 4 * (LON + ha) - eqtime)
    sunset = midnight + timedelta(minutes=720 - 4 * (LON - ha) - eqtime)
    return sunrise, sunset


def phase_at(now: datetime) -> str:
    local_day = now.astimezone(TZ).date()
    sunrise, sunset = sun_times(local_day)
    if now < sunrise - TWILIGHT or now >= sunset + TWILIGHT:
        return PHASE_NIGHT
    if now < sunrise + TWILIGHT:
        return PHASE_DAWN
    if now >= sunset - TWILIGHT:
        return PHASE_DUSK
    return PHASE_DAY


@dataclass(frozen=True)
class Outside:
    """Что сейчас за окном кабинета."""

    phase: str
    weather: str | None  # None — погоду узнать не удалось
    local_time: str  # «21:40» по Трансильвании

    @property
    def light(self) -> str:
        """Корзина света для снимка: пасмурная погода днём — как вечер."""
        if self.phase == PHASE_DAY and self.weather in _GLOOMY:
            return PHASE_DUSK
        return self.phase

    @property
    def key(self) -> str:
        return f"{self.light}/{self.weather or '-'}"

    def ru(self) -> str:
        parts = [f"{self.local_time} по Трансильвании", _PHASE_TEXT[self.phase][0]]
        if self.weather:
            parts.append(_WEATHER_TEXT[self.weather][0])
        return ", ".join(parts)

    def en(self) -> str:
        parts = [_PHASE_TEXT[self.light][1]]
        if self.weather and _WEATHER_TEXT[self.weather][1]:
            parts.append(_WEATHER_TEXT[self.weather][1])
        if self.phase == PHASE_DAY and self.weather in _GLOOMY:
            parts.append("gloomy dim room")
        return ", ".join(parts)


def _fetch_code_sync() -> int:
    with urllib.request.urlopen(WEATHER_URL, timeout=WEATHER_TIMEOUT_S) as resp:  # noqa: S310
        return int(json.loads(resp.read())["current"]["weather_code"])


class Transylvania:
    """Погода с кэшем; ``fetch`` подменяется в тестах."""

    def __init__(self, fetch=None, clock=time.monotonic) -> None:
        self._fetch = fetch or (lambda: asyncio.to_thread(_fetch_code_sync))
        self._clock = clock
        self._cached: tuple[float, str] | None = None

    async def weather(self) -> str | None:
        if self._cached is not None and self._clock() - self._cached[0] < WEATHER_TTL_S:
            return self._cached[1]
        try:
            kind = _weather_kind(await self._fetch())
        except Exception as exc:  # сеть, JSON, что угодно — без погоды
            log.info("transylvania: погода недоступна: %s", exc)
            return self._cached[1] if self._cached is not None else None
        self._cached = (self._clock(), kind)
        return kind

    async def outside(self, now: datetime) -> Outside:
        return Outside(
            phase=phase_at(now),
            weather=await self.weather(),
            local_time=now.astimezone(TZ).strftime("%H:%M"),
        )
