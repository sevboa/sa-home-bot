"""Время суток и погода за окном кабинета Альфреда (Этап 49.2).

Замок — в Трансильвании (точка — замок Бран), часы там реальные
(Europe/Bucharest). Светло или темно — по восходу и закату для даты, так что
световой день меняется с сезоном; погода — реальная, с Open-Meteo (без
ключа), кэш на час. Дождь, гроза, туман, снег днём дают «вечерний» свет.
Ночью в окне — луна по фазе (``astro.moon``), за сплошными облаками её нет.
Цель — устойчивая и загадочная обстановка, не метеосводка (решение
пользователя 2026-09-30): сбой погоды — просто без погоды.

Сеть — ``urllib`` + ``asyncio.to_thread``, как в net/searxng.py.
"""

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
LAT, LON = 45.515, 25.367  # замок Бран
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

# (по-русски — Альфреду и Ведущему, по-английски — в промпт снимка)
# Закат и ночь — ночная лаборатория 2026-10-09 (~/nightlab, 1520 кадров
# полного пути снимка): закат «с первыми свечами» читается закатом в 51%
# против 35%; ночь — тёплые свечи атмосферой и холодная луна в окне
# (тёплый+холодный свет 67% против 44%, ночь 96%). Свечи ПРЕДМЕТАМИ
# («candles on the desk») вытесняли главное в 23% кадров. День «cold blue
# daylight» читался ночью (61%) — день оставлен прежним.
_PHASE_TEXT = {
    PHASE_DAWN: ("раннее утро, за окном светает", "cold dawn light through the window"),
    PHASE_DAY: ("день", "daylight through the window"),
    PHASE_DUSK: ("сумерки, за окном закат", "orange sunset glow through window, first candles lit"),
    PHASE_NIGHT: ("ночь, горят свечи", "night, candlelit room, warm oil lamp glow"),
}
# Крупный план — свет только цветом: окно и свечи в промпте рисовались
# вместо предмета (стенд 2026-10-09, L3: главное 62/56/47% против 56/53/38%
# у прода днём/на закате/ночью). Погоды и луны тут нет — они «за окном».
# День — как у общего вида: без окна gemma узнавала день в 16–19% против
# 72% у прода (кандидат «bright daylight, cold blue shadows» — ночь 3).
_CLOSEUP_LIGHT = {
    PHASE_DAWN: "pale teal dawn light",
    PHASE_DAY: _PHASE_TEXT[PHASE_DAY][1],
    PHASE_DUSK: "warm orange sunset glow",
    PHASE_NIGHT: "night, cold blue moonlight, warm glow",
}
# Луна в окне по фазе (Outside.moon, astro.MOON_PHASES_RU). Стенд 2026-10-09:
# на общем виде и в окне фаза различима (луна видна 83% в полнолуние и серп,
# серп узнаётся 38%, в новолуние — звёзды), на селфи почти не видна.
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
# Сплошные облака — без слова moon: «moon hidden behind clouds» луну всё
# равно рисовал (71%).
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
    """Восход и закат (UTC) над Браном (``astro``, NOAA, точность пара минут)."""
    sd = astro.sun_day(LAT, LON, day)
    # Широта Брана — полярных дня и ночи не бывает, полдень — только для типов.
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
    """Что сейчас за окном кабинета."""

    phase: str
    weather: str | None  # None — погоду узнать не удалось
    local_time: str  # «21:40» по Трансильвании
    moon: str | None = None  # «растущий серп» — фаза луны, astro.MOON_PHASES_RU
    cloud_cover: int | None = None  # % облачности, None — неизвестно

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
        """Свет для промпта снимка. ``closeup`` — крупный план предмета: свет
        только цветом, без окна, свечей и погоды."""
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
    """(WMO weather code, % облачности)."""
    with urllib.request.urlopen(WEATHER_URL, timeout=WEATHER_TIMEOUT_S) as resp:  # noqa: S310
        cur = json.loads(resp.read())["current"]
    cloud = cur.get("cloud_cover")
    return int(cur["weather_code"]), int(cloud) if cloud is not None else None


class Transylvania:
    """Погода с кэшем; ``fetch`` подменяется в тестах — отдаёт код WMO или
    (код, % облачности)."""

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
        except Exception as exc:  # сеть, JSON, что угодно — без погоды
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
