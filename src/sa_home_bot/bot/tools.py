"""Инструменты (tool-calling) для диалога /ai — LLM_INTEGRATION_PLAN.md §7-8.

Каждый тул — узкая функция в явном реестре TOOL_HANDLERS, не общий прокси
на произвольное действие роя (§7.2 плана — общий прокси был бы дырой в
правах: модель дозвонилась бы куда угодно). Декларации (TOOL_DECLARATIONS)
— формат OpenAI function-calling, который Ollama понимает нативно для
tool-calling-моделей (qwen3 в их числе).

Погода, конвертер валют и калькулятор не ходят по протоколу роя вообще —
это не системные операции конкретной ноды (как apps/monitor), а либо
чистый расчёт, либо публичный API без ключа/состояния, одинаково доступный
с любой ноды. Выполняются прямо здесь (см. §8.4 плана — решение упростить
относительно первоначального черновика с отдельной службой "net").
Арифметику конвертера (сумма * курс) делает сам тул на Python, не второй
проход через тул calc — для одного умножения гонять его ещё раз через
модель не даёт выгоды в точности, только лишний круг.

``remind`` — единственный тул, ходящий по протоколу роя (в службу tasks,
см. sa_home_bot.tasks) — ставит отложенную задачу "спросить нейронку ещё
раз в момент X" (§8.5 плана, генерализовано 2026-07-24: раньше писал
готовый текст константным напоминанием прямо в БД бота, теперь сама
доставка — новый живой ответ модели, см. sa_home_bot.tasks.service).
Никакого доступа "в систему" — только создание такой задачи.

Этот модуль сознательно не зависит от aiogram — его импортирует не только
бот, но и служба tasks (см. докстринг ToolContext), которой Telegram не
нужен вовсе.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import math
import operator
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.tasks import protocol as task_protocol

log = logging.getLogger(__name__)

# Куда стрелять llm.chat для отложенных задач, создаваемых тулом remind —
# тот же узел/служба, что и живой /ai (bot/ai_flow.py::LLM_NODE/LLM_SERVICE).
# Продублировано здесь как литерал, а не импортировано оттуда: ai_flow.py
# сам импортирует этот модуль (bot.tools) — обратный импорт был бы циклом.
LLM_NODE = "winpc"
LLM_SERVICE = "llm"


@dataclass
class ToolContext:
    """``history`` — сообщения, которые ПРЯМО СЕЙЧАС видит модель (та же
    ссылка, что и ``messages`` в llm_chat.run_chat_loop, живая находка
    2026-07-24) — тул remind берёт снимок диалога отсюда, не из БД: у
    службы tasks (второй пользователь этого модуля, см. докстринг файла)
    нет доступа к ai_turns бота, а живому /ai читать БД ради того же самого
    незачем, раз список уже в памяти. ``node_link`` — только remind ходит
    по протоколу (в службу tasks); прочим тулам не нужен."""

    chat_id: int | None
    dialogue_id: int | None
    trigger_message_id: int | None
    settings: Settings
    node_link: ServiceLink | None = None
    history: list[dict[str, Any]] = field(default_factory=list)


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
# Живая находка 2026-07-24: реальная задача (площадь цилиндра, формула с π)
# показала, что без именованных констант модель вынуждена подставлять
# приближение "3.14159" сама (или вообще не звать тул) — добавлены pi/e как
# единственные разрешённые "переменные", не произвольные имена.
_ALLOWED_NAMES: dict[str, float] = {"pi": math.pi, "e": math.e}
_MAX_POW_EXPONENT = 1000  # защита от x**(огромное число) — не таймаут, а память/CPU
_ROUND_NDIGITS = 6  # "32.98672286269283" читается хуже, чем "32.986723"


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError("слишком большая степень")
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(
        "недопустимое выражение (разрешены только числа, pi, e и + - * / ** или ^ ())"
    )


async def tool_calc(ctx: ToolContext, args: dict[str, Any]) -> str:
    expr = args.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        return "ошибка: пустое выражение"
    # Живая находка 2026-07-24: модель пишет степень как в математике,
    # "1.5^2", не как в Python "1.5**2". У "^" в Python СОВСЕМ другой
    # приоритет операций (ниже "+", а не выше "*", как у степени) — трактовать
    # AST-узел BitXor напрямую как "**" ломает любое выражение сложнее
    # одного "a^b" (проверено: "2*pi*1.5^2+2*pi*1.5*2" вычислялось неверно).
    # Текстовая замена ДО парсинга — "^" в разрешённых выражениях больше
    # никогда и ни для чего другого не встречается, поэтому безопасна.
    expr = expr.replace("^", "**")
    try:
        tree = ast.parse(expr, mode="eval")
        value = _safe_eval(tree.body)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError, OverflowError) as exc:
        return f"ошибка вычисления: {exc}"
    if isinstance(value, float):
        if value.is_integer():
            value = int(value)
        else:
            # "32.98672286269283" — избыточная точность, которую персонаж
            # никогда бы не произнёс; округляем, не обрубая до неточности.
            value = round(value, _ROUND_NDIGITS)
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


# --- remind: единственный тул, ходящий по протоколу роя, см. докстринг модуля ---


async def tool_remind(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.chat_id is None or ctx.dialogue_id is None or ctx.trigger_message_id is None:
        return "ошибка: отложенные задачи недоступны вне диалога"
    if ctx.node_link is None:
        return "ошибка: служба задач недоступна"
    when_raw = args.get("when")
    text = args.get("text")
    if not isinstance(when_raw, str) or not when_raw.strip():
        return "ошибка: не указано время (when, ISO 8601)"
    if not isinstance(text, str) or not text.strip():
        return "ошибка: не указано, что сделать/сказать (text)"
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
    if due_at_utc <= datetime.now(tz=UTC):
        return "ошибка: указанное время уже прошло"

    # Директива дописывается в снимок ТЕКУЩЕЙ истории (ctx.history — то, что
    # модель видит прямо сейчас, см. докстринг ToolContext) — служба tasks
    # прогоняет ровно этот список через llm.chat заново в момент due_at, без
    # доступа к ai_turns бота (решение пользователя 2026-07-24: снимок
    # делается здесь, при создании задачи, а не реконструируется позже).
    # "Настало время" привязано к due_at, а не к моменту создания задачи —
    # due_at и есть момент фактического срабатывания (с точностью до
    # интервала опроса службы tasks).
    directive = (
        f"Настало время ({due_at:%Y-%m-%d %H:%M}), на которое тебя раньше "
        f"попросили сделать вот что: «{text.strip()}». Сделай/скажи это "
        "сейчас, от своего имени, в характере — как будто сам вспомнил, а не "
        "отвечаешь на прямой вопрос. Если нужно что-то посчитать или узнать "
        "(погоду, курс) — пользуйся инструментами, не полагайся на память."
    )
    task_args = {
        "messages": [*ctx.history, {"role": "user", "content": directive}],
        "tools": TOOL_DECLARATIONS,
        "think": ctx.settings.llm.think_chat,
        "chat_id": ctx.chat_id,
    }
    meta = {
        "kind": task_protocol.TASK_KIND_LLM_CHAT,
        "chat_id": ctx.chat_id,
        "dialogue_id": ctx.dialogue_id,
        "trigger_message_id": ctx.trigger_message_id,
    }
    dst = Address(node=task_protocol.NODE_ID, service=task_protocol.SERVICE_NAME)
    try:
        await ctx.node_link.command(
            task_protocol.ACTION_CREATE,
            {
                "due_at": due_at_utc.isoformat(),
                "dst_node": LLM_NODE,
                "dst_service": LLM_SERVICE,
                "action": task_protocol.ACTION_CHAT_LOOP,
                "args": task_args,
                "timeout_s": ctx.settings.llm.request_timeout_s,
                "meta": meta,
            },
            dst=dst,
        )
    except (ServiceUnavailableError, ProtoError) as exc:
        return f"внутренняя ошибка: не удалось поставить задачу ({exc})"
    return f"задача поставлена на {due_at.strftime('%Y-%m-%d %H:%M')} (местное время)"


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
                "Точно вычислить арифметическое выражение (числа, + - * / скобки, "
                "степень как ** или ^, плюс константы pi и e — без произвольных "
                "переменных и функций). Используй для ЛЮБОЙ реальной арифметики, "
                "включая формулы (площадь, объём и т.п.) — подставь известные "
                "числа и pi/e в выражение и вызови тул, не считай и не "
                "подставляй в уме."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Например: 2 * pi * 1.5 * (1.5 + 2) или 1.5^2",
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
                "Поставить отложенную задачу в этом же чате на конкретный момент "
                "времени — НЕ готовый текст, а то, что нужно СДЕЛАТЬ или СКАЗАТЬ, "
                "когда время наступит: в этот момент тебя вызовут заново и ты сам "
                "сформулируешь ответ, при необходимости пользуясь другими "
                "инструментами (например, посмотреть погоду именно в тот момент, "
                "а не сейчас). Переведи то, что попросил пользователь ('через 20 "
                "минут', 'завтра в 9 утра'), в точную дату-время сам, используя "
                "текущее время из контекста разговора."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "Точная дата-время в ISO 8601, например 2026-07-24T21:30:00",
                    },
                    "text": {
                        "type": "string",
                        "description": "Что нужно сделать или сказать в момент срабатывания",
                    },
                },
                "required": ["when", "text"],
            },
        },
    },
]
