from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sa_home_bot import astro

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Bucharest")
LAT, LON = 45.515, 25.367
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}&current=weather_code,cloud_cover"
    "&timezone=Europe%2FBucharest"
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

_PHASE_TEXT = {
    PHASE_DAWN: ("раннее утро, за окном светает", "cold dawn light through the window"),
    PHASE_DAY: ("день", "daylight through the window"),
    PHASE_DUSK: ("сумерки, за окном закат", "orange sunset glow through window, first candles lit"),
    PHASE_NIGHT: ("ночь, горят свечи", "night, candlelit room, warm oil lamp glow"),
}
_CLOSEUP_LIGHT = {
    PHASE_DAWN: "pale teal dawn light",
    PHASE_DAY: _PHASE_TEXT[PHASE_DAY][1],
    PHASE_DUSK: "warm orange sunset glow",
    PHASE_NIGHT: "night, cold blue moonlight, warm glow",
}
_MOON_EN = {
    "полнолуние": "bright full moon in the window, strong blue moonlight",
    "растущая луна": "bright moon in the window, blue moonlight",
    "убывающая луна": "bright moon in the window, blue moonlight",
    "первая четверть": "half moon in the window, blue moonlight",
    "последняя четверть": "half moon in the window, blue moonlight",
    "растущий серп": "thin crescent moon in the window, faint moonlight",
    "убывающий серп": "thin crescent moon in the window, faint moonlight",
    "новолуние": "starry night sky in the window",
}
_MOON_DEFAULT_EN = "cold blue moonlight through the window"
_MOON_CLOUDY_EN = "dark cloudy night sky in the window"
CLOUDY_PCT = 70
_WEATHER_TEXT = {
    WEATHER_CLEAR: ("ясно", ""),
    WEATHER_CLOUDY: ("пасмурно", "overcast sky"),
    WEATHER_FOG: ("туман за окном", "thick fog outside the window"),
    WEATHER_RAIN: ("дождь стучит в окно", "rain on the window"),
    WEATHER_SNOW: ("за окном снег", "snow falling outside the window"),
    WEATHER_STORM: ("гроза", "thunderstorm, lightning outside the window"),
}


def _weather_kind(code: int) -> str:
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
    return WEATHER_RAIN


def sun_times(day: date) -> tuple[datetime, datetime]:
    sd = astro.sun_day(LAT, LON, day)
    return sd.sunrise or sd.noon, sd.sunset or sd.noon


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
    phase: str
    weather: str | None
    local_time: str
    moon: str | None = None
    cloud_cover: int | None = None

    @property
    def light(self) -> str:
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
        if self.moon and self.phase == PHASE_NIGHT:
            parts.append(f"луна: {self.moon}")
        return ", ".join(parts)

    def _night_sky_en(self) -> str:
        cloudy = (
            self.cloud_cover > CLOUDY_PCT
            if self.cloud_cover is not None
            else self.weather in _GLOOMY or self.weather == WEATHER_CLOUDY
        )
        if cloudy:
            return _MOON_CLOUDY_EN
        return _MOON_EN.get(self.moon or "", _MOON_DEFAULT_EN)

    def en(self, *, closeup: bool = False) -> str:
        if closeup:
            return _CLOSEUP_LIGHT[self.light]
        parts = [_PHASE_TEXT[self.light][1]]
        if self.light == PHASE_NIGHT:
            parts.append(self._night_sky_en())
        if self.weather and _WEATHER_TEXT[self.weather][1]:
            parts.append(_WEATHER_TEXT[self.weather][1])
        if self.phase == PHASE_DAY and self.weather in _GLOOMY:
            parts.append("gloomy dim room")
        return ", ".join(parts)


def _fetch_code_sync() -> tuple[int, int | None]:
    with urllib.request.urlopen(WEATHER_URL, timeout=WEATHER_TIMEOUT_S) as resp:  # noqa: S310
        cur = json.loads(resp.read())["current"]
    cloud = cur.get("cloud_cover")
    return int(cur["weather_code"]), int(cloud) if cloud is not None else None


class Transylvania:
    def __init__(self, fetch=None, clock=time.monotonic) -> None:
        self._fetch = fetch or (lambda: asyncio.to_thread(_fetch_code_sync))
        self._clock = clock
        self._cached: tuple[float, str, int | None] | None = None

    async def _sky(self) -> tuple[str, int | None] | None:
        if self._cached is not None and self._clock() - self._cached[0] < WEATHER_TTL_S:
            return self._cached[1], self._cached[2]
        try:
            raw = await self._fetch()
            code, cloud = raw if isinstance(raw, tuple) else (raw, None)
            kind = _weather_kind(code)
        except Exception as exc:
            log.info("transylvania: погода недоступна: %s", exc)
            return (self._cached[1], self._cached[2]) if self._cached is not None else None
        self._cached = (self._clock(), kind, cloud)
        return kind, cloud

    async def weather(self) -> str | None:
        sky = await self._sky()
        return sky[0] if sky is not None else None

    async def outside(self, now: datetime) -> Outside:
        sky = await self._sky()
        return Outside(
            phase=phase_at(now),
            weather=sky[0] if sky is not None else None,
            local_time=now.astimezone(TZ).strftime("%H:%M"),
            moon=astro.moon(now).phase_ru,
            cloud_cover=sky[1] if sky is not None else None,
        )
