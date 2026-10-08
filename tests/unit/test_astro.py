"""astro: солнце и луна без сети (опорные значения — таблицы восхода/фаз)."""

from datetime import UTC, date, datetime

from sa_home_bot import astro

ALMATY = (43.24, 76.95)


def test_moon_phases_on_known_dates():
    new = astro.moon(datetime(2026, 10, 10, 15, 50, tzinfo=UTC))
    full = astro.moon(datetime(2026, 10, 26, 4, 12, tzinfo=UTC))
    first = astro.moon(datetime(2026, 10, 18, 16, 13, tzinfo=UTC))
    assert new.illumination < 0.01 and new.phase_ru == "новолуние"
    assert astro.moon(datetime(2026, 10, 8, 20, 0, tzinfo=UTC)).phase_ru == "убывающий серп"
    assert full.illumination > 0.99 and full.phase_ru == "полнолуние"
    assert abs(first.illumination - 0.5) < 0.02 and first.waxing
    assert first.phase_ru == "первая четверть"


def test_sun_day_and_noon_altitude():
    day = astro.sun_day(*ALMATY, date(2026, 10, 9))
    assert day.sunrise is not None and day.sunset is not None
    assert day.sunrise < day.noon < day.sunset
    altitude, azimuth = astro.sun_position(*ALMATY, day.noon)
    # полдень: 90 − широта + склонение (≈ −6° в начале октября), солнце на юге
    assert 39 < altitude < 43
    assert 175 < azimuth < 185


def test_polar_day_has_no_sunrise():
    day = astro.sun_day(78.0, 15.0, date(2026, 6, 21))
    assert day.sunrise is None and day.sunset is None


def test_light_phase_by_altitude_and_side():
    assert astro.light_phase(-20, 30) == astro.LIGHT_NIGHT
    assert astro.light_phase(2, 100) == astro.LIGHT_DAWN
    assert astro.light_phase(2, 260) == astro.LIGHT_DUSK
    assert astro.light_phase(30, 180) == astro.LIGHT_DAY
