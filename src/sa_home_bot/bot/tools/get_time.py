from __future__ import annotations

import itertools
import json
import logging
from datetime import UTC, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sa_home_bot import astro
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec
from sa_home_bot.utils.clock import utc_now
from sa_home_bot.utils.timefmt import zone_or_none

log = logging.getLogger(__name__)


WEEKDAYS_RU = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _hhmm(moment: datetime | None, zone: ZoneInfo | timezone) -> str | None:
    return moment.astimezone(zone).strftime("%H:%M") if moment is not None else None


def place_now(lat: float, lon: float, zone: ZoneInfo | timezone, now: datetime) -> dict[str, Any]:
    local = now.astimezone(zone)
    altitude, azimuth = astro.sun_position(lat, lon, now)
    day = astro.sun_day(lat, lon, local.date())
    moon = astro.moon(now)
    return {
        "local_time": local.strftime("%H:%M"),
        "date": local.strftime("%Y-%m-%d"),
        "weekday": WEEKDAYS_RU[local.weekday()],
        "utc_offset": local.strftime("%z"),
        "time_of_day": astro.LIGHT_RU[astro.light_phase(altitude, azimuth)],
        "sun": {
            "altitude_deg": round(altitude, 1),
            "azimuth_deg": round(azimuth),
            "sunrise": _hhmm(day.sunrise, zone),
            "sunset": _hhmm(day.sunset, zone),
            "solar_noon": _hhmm(day.noon, zone),
        },
        "moon": {
            "phase": moon.phase_ru,
            "illumination_pct": round(moon.illumination * 100),
        },
    }


_PLACE_TIMEZONES: dict[str, str] = {
    "москва": "Europe/Moscow",
    "россия": "Europe/Moscow",
    "казахстан": "Asia/Almaty",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "киев": "Europe/Kyiv",
    "украина": "Europe/Kyiv",
    "минск": "Europe/Minsk",
    "беларусь": "Europe/Minsk",
    "ташкент": "Asia/Tashkent",
    "узбекистан": "Asia/Tashkent",
    "лондон": "Europe/London",
    "великобритания": "Europe/London",
    "англия": "Europe/London",
    "гринвич": "UTC",
    "гмт": "UTC",
    "gmt": "UTC",
    "utc": "UTC",
    "нью-йорк": "America/New_York",
    "сша": "America/New_York",
    "италия": "Europe/Rome",
    "рим": "Europe/Rome",
    "германия": "Europe/Berlin",
    "берлин": "Europe/Berlin",
    "франция": "Europe/Paris",
    "париж": "Europe/Paris",
    "испания": "Europe/Madrid",
    "мадрид": "Europe/Madrid",
    "турция": "Europe/Istanbul",
    "стамбул": "Europe/Istanbul",
    "оаэ": "Asia/Dubai",
    "дубай": "Asia/Dubai",
    "китай": "Asia/Shanghai",
    "пекин": "Asia/Shanghai",
    "япония": "Asia/Tokyo",
    "токио": "Asia/Tokyo",
    "индия": "Asia/Kolkata",
}


def _format_hours_diff(minutes: int) -> str:
    hours, mins = divmod(abs(minutes), 60)
    return f"{hours} ч" if mins == 0 else f"{hours} ч {mins} мин"


def _place_entry(place: str, tz_name: str, target: datetime) -> dict[str, str]:
    offset = target.strftime("%z")
    return {
        "place": place,
        "timezone": tz_name,
        "utc_offset": f"{offset[:3]}:{offset[3:]}",
        "local_time": target.strftime("%Y-%m-%d %H:%M"),
        "weekday": WEEKDAYS_RU[target.weekday()],
    }


async def tool_get_time(ctx: ToolContext, args: dict[str, Any]) -> str:
    places_raw = args.get("places")
    if isinstance(places_raw, list) and places_raw:
        place_list = [p.strip() for p in places_raw if isinstance(p, str) and p.strip()]
    else:
        single = args.get("place")
        if not isinstance(single, str) or not single.strip():
            return "ошибка: не указано место (place или places)"
        place_list = [single.strip()]
    if not place_list:
        return "ошибка: не указано место (place или places)"

    at_raw = args.get("at")
    if isinstance(at_raw, str) and at_raw.strip():
        try:
            at = datetime.fromisoformat(at_raw)
        except ValueError:
            return "ошибка: 'at' должен быть в формате ISO 8601, например 2026-07-24T20:28:00+03:00"
        if at.tzinfo is None:
            return "ошибка: 'at' должен содержать смещение часового пояса (например +03:00)"
        at_utc = at.astimezone(UTC)
    else:
        at_utc = utc_now()

    resolved: list[tuple[str, str, datetime]] = []
    unknown: list[str] = []
    for place in place_list:
        tz_name = _PLACE_TIMEZONES.get(place.lower())
        if tz_name is None:
            unknown.append(place)
            continue
        zone = zone_or_none(tz_name)
        if zone is None:
            log.warning("tool_get_time: система не знает часовой пояс %s", tz_name)
            return f"внутренняя ошибка: не удалось определить часовой пояс {tz_name}"
        resolved.append((place, tz_name, at_utc.astimezone(zone)))

    if not resolved:
        return (
            f"не знаю часовой пояс для «{', '.join(unknown)}» — "
            "не могу посчитать точно, не додумывай"
        )

    entries = [_place_entry(place, tz_name, target) for place, tz_name, target in resolved]
    if len(place_list) == 1:
        return json.dumps(entries[0], ensure_ascii=False)
    payload: dict[str, Any] = {"places": entries}
    if unknown:
        payload["unknown_places"] = unknown

    diffs = []
    for (place_a, _, target_a), (place_b, _, target_b) in itertools.combinations(resolved, 2):
        delta_minutes = round((target_b.utcoffset() - target_a.utcoffset()).total_seconds() / 60)
        if delta_minutes == 0:
            diffs.append(f"{place_a} и {place_b}: одинаковое время")
        elif delta_minutes > 0:
            diffs.append(f"{place_b} впереди {place_a} на {_format_hours_diff(delta_minutes)}")
        else:
            diffs.append(f"{place_a} впереди {place_b} на {_format_hours_diff(delta_minutes)}")
    payload["differences"] = diffs
    return json.dumps(payload, ensure_ascii=False)


_DECL_TIME: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": (
            "Точно узнать текущее время (и день недели) в конкретном "
            "городе/стране, а также разницу во времени между НЕСКОЛЬКИМИ "
            "местами. Часовые пояса и разницу между ними НЕ считай сам — "
            "модель на практике их путает и противоречит сама себе даже "
            "после того, как назвала верные названия поясов. Используй "
            "этот тул для ЛЮБОГО вопроса про время не 'у нас/сейчас' (то "
            "уже есть в контексте разговора), а в другом городе/стране — "
            "ВКЛЮЧАЯ короткие вопросы-продолжения вроде 'а в Х?' сразу "
            "после уже заданного вопроса про другое место: это НОВОЕ "
            "место, вызови тул заново, не выводи по аналогии с прошлым "
            "ответом. Если спрашивают РАЗНИЦУ между двумя и более местами "
            "(или список часовых поясов сразу для нескольких мест) — "
            "передай ВСЕ места в places одним вызовом, тул сам посчитает "
            "разницу (поле differences в ответе) — НЕ вычитай время двух "
            "мест сам. Если тул не знает место — так и скажи как есть, не "
            "досчитывай и не придумывай сам."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "place": {
                    "type": "string",
                    "description": (
                        "Город или страна (например: Москва) — для ОДНОГО места. "
                        "Если мест несколько (сравнение/разница) — используй places."
                    ),
                },
                "places": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        'Список мест (например: ["Москва", "Италия"]) — для '
                        "сравнения нескольких мест или вопроса про разницу во "
                        "времени между ними. Если задан, place игнорируется."
                    ),
                },
                "at": {
                    "type": "string",
                    "description": (
                        "Необязательно: конкретный момент времени в ISO 8601 "
                        "СО смещением (например 2026-08-01T12:00:00+03:00) — "
                        "для вопросов про другую дату, не 'сейчас'. Без этого "
                        "поля берётся текущий момент."
                    ),
                },
            },
        },
    },
}


GET_TIME = ToolSpec(name="get_time", handler=tool_get_time, declaration=_DECL_TIME)
