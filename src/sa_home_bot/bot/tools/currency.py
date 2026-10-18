from __future__ import annotations

import json
import logging
import time
import urllib.parse
from typing import Any

from sa_home_bot.bot.tools.core import ToolContext, ToolSpec
from sa_home_bot.utils.http import JSON_FETCH_ERRORS, get_json_async

log = logging.getLogger(__name__)


_HTTP_TIMEOUT_S = 10.0


_RATES_TTL_S = 3600.0
_RATES_CACHE: dict[str, tuple[float, dict[str, float]]] = {}


async def _get_rates(base: str) -> dict[str, float] | None:
    now = time.monotonic()
    cached = _RATES_CACHE.get(base)
    if cached is not None and now - cached[0] < _RATES_TTL_S:
        return cached[1]
    url = f"https://open.er-api.com/v6/latest/{urllib.parse.quote(base)}"
    try:
        data = await get_json_async(url, _HTTP_TIMEOUT_S)
    except JSON_FETCH_ERRORS as exc:
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


_DECL_CURRENCY: dict[str, Any] = {
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
}


CONVERT_CURRENCY = ToolSpec(
    name="convert_currency", handler=tool_convert_currency, declaration=_DECL_CURRENCY
)
