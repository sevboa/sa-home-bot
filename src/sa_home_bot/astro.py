"""Солнце и луна по координатам и моменту — локально, без сети.

Нужны туле погоды (``bot/tools.py::tool_get_weather`` — «что сейчас в городе»)
и окну кабинета Альфреда (``bot/interactives/transylvania.py``). Как и с
часовыми поясами, не поручаем это модели: высота солнца, восход/закат и фаза
луны считаются детерминированно.

Формулы — NOAA (солнце, точность ~1 мин по времени и доли градуса по углу) и
укороченный Meeus, гл. 48 (освещённость луны, точность ~1%). Рефракция у
горизонта учтена стандартным −0.833° для восхода/заката.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

# Высота солнца, ниже которой — ночь (конец гражданских сумерек), и выше
# которой — полноценный день (кончился «золотой час» у горизонта).
CIVIL_TWILIGHT_DEG = -6.0
GOLDEN_HOUR_DEG = 6.0

LIGHT_NIGHT = "night"
LIGHT_DAWN = "dawn"
LIGHT_DAY = "day"
LIGHT_DUSK = "dusk"

LIGHT_RU = {
    LIGHT_NIGHT: "ночь",
    LIGHT_DAWN: "рассвет",
    LIGHT_DAY: "день",
    LIGHT_DUSK: "закат",
}

MOON_PHASES_RU = (
    "новолуние",
    "растущий серп",
    "первая четверть",
    "растущая луна",
    "полнолуние",
    "убывающая луна",
    "последняя четверть",
    "убывающий серп",
)

_SYNODIC_DAYS = 29.530588853


def _noaa(when: datetime) -> tuple[float, float]:
    """(уравнение времени в минутах, склонение солнца в радианах) на момент."""
    t = when.astimezone(UTC)
    n = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    gamma = 2 * math.pi / 365 * (n - 1 + (hour - 12) / 24)
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
    return eqtime, decl


def sun_position(lat: float, lon: float, when: datetime) -> tuple[float, float]:
    """(высота над горизонтом, азимут от севера по часовой) в градусах."""
    eqtime, decl = _noaa(when)
    t = when.astimezone(UTC)
    minutes = t.hour * 60 + t.minute + t.second / 60
    true_solar = (minutes + eqtime + 4 * lon) % 1440
    ha = math.radians(true_solar / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    altitude = 90 - math.degrees(zen)
    az = math.degrees(
        math.atan2(math.sin(ha), math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))
    )
    return altitude, (az + 180) % 360


@dataclass(frozen=True)
class SunDay:
    """Восход, полдень, закат (UTC). ``None`` у восхода/заката — полярный
    день или ночь: солнце в эти сутки не пересекает горизонт."""

    sunrise: datetime | None
    noon: datetime
    sunset: datetime | None


def sun_day(lat: float, lon: float, day: date) -> SunDay:
    """Восход и закат для календарной даты ``day`` (UTC-сутки этой даты)."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    eqtime, decl = _noaa(midnight + timedelta(hours=12))
    noon = midnight + timedelta(minutes=720 - 4 * lon - eqtime)
    phi = math.radians(lat)
    cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl)) - math.tan(
        phi
    ) * math.tan(decl)
    if not -1.0 <= cos_ha <= 1.0:
        return SunDay(None, noon, None)
    ha = math.degrees(math.acos(cos_ha))
    return SunDay(noon - timedelta(minutes=4 * ha), noon, noon + timedelta(minutes=4 * ha))


def light_phase(altitude: float, azimuth: float) -> str:
    """Ночь / рассвет / день / закат по высоте солнца; утро от вечера —
    по стороне неба (восток — до полудня)."""
    if altitude < CIVIL_TWILIGHT_DEG:
        return LIGHT_NIGHT
    if altitude < GOLDEN_HOUR_DEG:
        return LIGHT_DAWN if azimuth < 180 else LIGHT_DUSK
    return LIGHT_DAY


@dataclass(frozen=True)
class Moon:
    illumination: float  # 0..1, доля освещённого диска
    age_days: float  # дней с новолуния
    waxing: bool

    @property
    def phase_ru(self) -> str:
        # По освещённости, как в календарях: новолуние/полнолуние и четверти —
        # узкие окна, между ними серп (меньше половины) и луна (больше).
        lit = self.illumination
        if lit < 0.03:
            return MOON_PHASES_RU[0]
        if lit > 0.97:
            return MOON_PHASES_RU[4]
        if abs(lit - 0.5) < 0.07:
            return MOON_PHASES_RU[2] if self.waxing else MOON_PHASES_RU[6]
        if lit < 0.5:
            return MOON_PHASES_RU[1] if self.waxing else MOON_PHASES_RU[7]
        return MOON_PHASES_RU[3] if self.waxing else MOON_PHASES_RU[5]


def moon(when: datetime) -> Moon:
    """Фаза луны (Meeus, гл. 48, укороченно)."""
    t = when.astimezone(UTC)
    jd = t.timestamp() / 86400 + 2440587.5
    tc = (jd - 2451545.0) / 36525
    d = math.radians((297.8501921 + 445267.1114034 * tc) % 360)  # элонгация
    m = math.radians((357.5291092 + 35999.0502909 * tc) % 360)  # аномалия солнца
    mp = math.radians((134.9633964 + 477198.8675055 * tc) % 360)  # аномалия луны
    i = (
        180
        - math.degrees(d)
        - 6.289 * math.sin(mp)
        + 2.100 * math.sin(m)
        - 1.274 * math.sin(2 * d - mp)
        - 0.658 * math.sin(2 * d)
        - 0.214 * math.sin(2 * mp)
        - 0.110 * math.sin(d)
    )
    illum = (1 + math.cos(math.radians(i))) / 2
    elong = (180 - i) % 360  # 0 — новолуние, 180 — полнолуние
    return Moon(
        illumination=illum,
        age_days=elong / 360 * _SYNODIC_DAYS,
        waxing=elong < 180,
    )
