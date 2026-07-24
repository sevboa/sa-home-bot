"""Инструменты (tool-calling) для диалога /ai — LLM_INTEGRATION_PLAN.md §7-8.

Каждый тул — узкая функция в явном реестре TOOL_HANDLERS, не общий прокси
на произвольное действие роя (§7.2 плана — общий прокси был бы дырой в
правах: модель дозвонилась бы куда угодно). Все тулы здесь read-only,
кроме ``remind`` — тот пишет только в свою узкую табличку "отложенное
сообщение самому себе", не управляет системой (см. §8.5). Декларации
(TOOL_DECLARATIONS) — формат OpenAI function-calling, который Ollama
понимает нативно для tool-calling-моделей (qwen3 в их числе).

Погода, конвертер валют и калькулятор не ходят по протоколу роя вообще —
это не системные операции конкретной ноды (как apps/monitor), а либо
чистый расчёт, либо публичный API без ключа/состояния, одинаково доступный
с любой ноды. Выполняются прямо здесь, в процессе бота (см. §8.4 плана —
решение упростить относительно первоначального черновика с отдельной
службой "net"). Арифметику конвертера (сумма * курс) делает сам тул на
Python, не второй проход через тул calc — для одного умножения гонять его
ещё раз через модель не даёт выгоды в точности, только лишний круг.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import operator
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    chat_id: int | None
    store: Store
    settings: Settings


ToolHandler = Callable[["ToolContext", dict[str, Any]], Awaitable[str]]


# --- calc: без сети и без роя, ast с белым списком узлов (не eval()) ---

_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_MAX_POW_EXPONENT = 1000  # защита от x**(огромное число) — не таймаут, а память/CPU


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError("слишком большая степень")
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("недопустимое выражение (разрешены только числа и + - * / ** ())")


async def tool_calc(ctx: ToolContext, args: dict[str, Any]) -> str:
    expr = args.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        return "ошибка: пустое выражение"
    try:
        tree = ast.parse(expr, mode="eval")
        value = _safe_eval(tree.body)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError, OverflowError) as exc:
        return f"ошибка вычисления: {exc}"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


# --- HTTP-обвязка, общая для get_weather и convert_currency ниже: оба —
# публичные API без ключа/состояния, вызывает сам бот-процесс (не системные
# операции конкретной ноды, как apps/monitor — одинаково доступны с любой
# ноды, отдельная служба под них не нужна, см. §8.4 плана). ---

_HTTP_TIMEOUT_S = 10.0


def _get_json_sync(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — фиксированные публичные host'ы
        return json.loads(resp.read())


# --- get_weather ---
#
# Координаты города не просит у пользователя/модели напрямую — небольшая
# локальная модель не гарантированно точна в географических фактах (может
# перепутать широту/долготу или город). Вместо этого город из конфига
# ([weather].city) резолвится через геокодинг-API того же провайдера
# (Open-Meteo, без ключа, тот же трюк, что и сам прогноз) — детерминированно,
# не полагаясь на "память" модели. Результат кэшируется на время жизни
# процесса (_GEOCODE_CACHE) — город из конфига не меняется на лету (конфиг
# читается один раз при старте), незачем резолвить его на каждый запрос.

_GEOCODE_CACHE: dict[str, tuple[float, float, str]] = {}


async def _resolve_city(city: str) -> tuple[float, float, str] | None:
    """(latitude, longitude, отображаемое название) или None — город не
    найден геокодером, либо сам геокодер недоступен."""
    key = city.strip().lower()
    if key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[key]
    url = (
        "https://geocoding-api.open-meteo.com/v1/search"
        f"?name={urllib.parse.quote(city)}&count=1&language=ru&format=json"
    )
    try:
        data = await asyncio.to_thread(_get_json_sync, url, _HTTP_TIMEOUT_S)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("tool_get_weather: геокодирование «%s» не удалось: %s", city, exc)
        return None
    results = data.get("results") or []
    if not results:
        return None
    top = results[0]
    label = top.get("name", city)
    if top.get("country"):
        label = f"{label}, {top['country']}"
    resolved = (top["latitude"], top["longitude"], label)
    _GEOCODE_CACHE[key] = resolved
    return resolved


async def tool_get_weather(ctx: ToolContext, args: dict[str, Any]) -> str:
    # Живой баг 2026-07-24: декларация раньше не принимала город вообще
    # ("узнать погоду ДОМА") — модель на прямой вопрос про другой город
    # честно отказывала, а не молчаливо путала его с домом. args["city"] —
    # необязательный: без него — прежнее поведение (город из конфига).
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
        "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
        "&timezone=auto"
    )
    try:
        data = await asyncio.to_thread(_get_json_sync, url, _HTTP_TIMEOUT_S)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("tool_get_weather: %s", exc)
        return "не удалось получить погоду — сервис недоступен, повтори позже"
    current = data.get("current", {})
    return json.dumps(
        {
            "location": label,
            "temperature_c": current.get("temperature_2m"),
            "feels_like_c": current.get("apparent_temperature"),
            "wind_speed_kmh": current.get("wind_speed_10m"),
            "weather_code": current.get("weather_code"),
        },
        ensure_ascii=False,
    )


# --- convert_currency ---
#
# Умножение делает сам тул (обычный Python), не второй раунд через calc —
# для одной операции "сумма * курс" гонять её ещё и через модель незачем,
# только лишний круг генерации без выгоды в точности. Курсы — тоже не из
# "памяти" модели (устаревают за часы-дни), а с открытого API без ключа
# (open.er-api.com — рыночные курсы, ~160 валют, включая RUB/KZT и т.п.),
# кэшируются на _RATES_TTL_S — курсы обновляются на источнике не чаще
# раза в сутки, кэш на час экономит сеть, не портя актуальность на глаз.

_RATES_TTL_S = 3600.0
_RATES_CACHE: dict[str, tuple[float, dict[str, float]]] = {}


async def _get_rates(base: str) -> dict[str, float] | None:
    """Курсы всех валют за 1 единицу ``base``, или None — база не найдена
    сервисом, либо сам сервис недоступен."""
    now = time.monotonic()
    cached = _RATES_CACHE.get(base)
    if cached is not None and now - cached[0] < _RATES_TTL_S:
        return cached[1]
    url = f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base)}"
    try:
        data = await asyncio.to_thread(_get_json_sync, url, _HTTP_TIMEOUT_S)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("tool_convert_currency: %s", exc)
        return None
    if data.get("result") != "success":
        return None
    rates = data.get("rates")
    if not isinstance(rates, dict):
        return None
    _RATES_CACHE[base] = (now, rates)
    return rates


async def tool_convert_currency(ctx: ToolContext, args: dict[str, Any]) -> str:
    amount = args.get("amount")
    from_raw = args.get("from")
    to_raw = args.get("to")
    if not isinstance(amount, int | float):
        return "ошибка: 'amount' должен быть числом"
    if not isinstance(from_raw, str) or not from_raw.strip():
        return "ошибка: не указана исходная валюта (from)"
    if not isinstance(to_raw, str) or not to_raw.strip():
        return "ошибка: не указана целевая валюта (to)"
    from_code = from_raw.strip().upper()
    to_code = to_raw.strip().upper()
    rates = await _get_rates(from_code)
    if rates is None:
        return "не удалось получить курс валют — сервис недоступен, повтори позже"
    rate = rates.get(to_code)
    if rate is None:
        return (
            f"неизвестный код валюты «{to_code}» или «{from_code}» "
            "(нужен формат ISO 4217, например USD, RUB, KZT)"
        )
    return json.dumps(
        {
            "amount": amount,
            "from": from_code,
            "to": to_code,
            "rate": rate,
            "result": round(amount * rate, 4),
        },
        ensure_ascii=False,
    )


# --- remind: единственный пишущий тул, см. докстринг модуля ---


async def tool_remind(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.chat_id is None:
        return "ошибка: напоминания недоступны вне чата"
    when_raw = args.get("when")
    text = args.get("text")
    if not isinstance(when_raw, str) or not when_raw.strip():
        return "ошибка: не указано время (when, ISO 8601)"
    if not isinstance(text, str) or not text.strip():
        return "ошибка: не указан текст напоминания"
    try:
        due_at = datetime.fromisoformat(when_raw)
    except ValueError:
        return "ошибка: 'when' должен быть в формате ISO 8601, например 2026-07-24T21:30:00"
    # Наивную дату-время (без смещения) считаем локальным временем процесса —
    # именно в нём отдана строка "текущее время" в контексте промпта
    # (bot/ai_flow.py::_build_context_note), так что модель обычно отвечает
    # тем же способом, без явного смещения.
    if due_at.tzinfo is None:
        due_at = due_at.astimezone()
    due_at_utc = due_at.astimezone(UTC)
    now = datetime.now(tz=UTC)
    if due_at_utc <= now:
        return "ошибка: указанное время уже прошло"
    await ctx.store.create_reminder(ctx.chat_id, text.strip(), due_at_utc, now)
    return f"напоминание создано на {due_at.strftime('%Y-%m-%d %H:%M')} (местное время)"


TOOL_HANDLERS: dict[str, ToolHandler] = {
    "calc": tool_calc,
    "get_weather": tool_get_weather,
    "convert_currency": tool_convert_currency,
    "remind": tool_remind,
}

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": (
                "Точно вычислить арифметическое выражение (числа, + - * / ** и скобки, "
                "без переменных и функций). Используй для любой реальной арифметики — "
                "не считай в уме, если можно вызвать это."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Например: (2 + 3) * 4 / 7",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Узнать текущую погоду (температура, ощущается как, ветер) в любом "
                "городе мира — не только дома. Если пользователь называет город, "
                "передай его в city; если спрашивает просто 'какая погода' без "
                "уточнения — не передавай city вовсе, вернётся погода дома."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "Город, если он назван явно (например: Алматы)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "convert_currency",
            "description": (
                "Точно перевести сумму из одной валюты в другую по актуальному курсу. "
                "Используй для любого вопроса про курс/конвертацию денег — не пытайся "
                "вспомнить курс сам, он быстро устаревает."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Сумма для перевода"},
                    "from": {
                        "type": "string",
                        "description": "Код исходной валюты, ISO 4217 (например USD)",
                    },
                    "to": {
                        "type": "string",
                        "description": "Код целевой валюты, ISO 4217 (например RUB)",
                    },
                },
                "required": ["amount", "from", "to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remind",
            "description": (
                "Поставить напоминание в этом же чате на конкретный момент времени. "
                "Переведи то, что попросил пользователь ('через 20 минут', 'завтра "
                "в 9 утра'), в точную дату-время сам, используя текущее время из "
                "контекста разговора."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "Точная дата-время в ISO 8601, например 2026-07-24T21:30:00",
                    },
                    "text": {"type": "string", "description": "О чём напомнить"},
                },
                "required": ["when", "text"],
            },
        },
    },
]
