"""Bounded retry для сетевых вызовов Telegram Bot API на старте бота.

Живой инцидент 2026-08-12: после ребута alfred DNS для api.telegram.org уже
резолвился (это единственное, что проверяет ExecStartPre юнита), но сам HTTP
до Telegram ещё таймаутил — единственный незащищённый вызов bot.get_me() в
app.py падал необработанным TelegramNetworkError и убивал весь процесс.
Цикл получения апдейтов (aiogram Dispatcher.start_polling) сам ретраит такие
ошибки с backoff — эта проблема касалась только разовых вызовов ДО старта
поллинга (get_me, get_chat в validate_on_startup).

Ручной цикл (без tenacity — машина слабая, лишняя зависимость не нужна) по
образцу llm/ollama.py::_post_with_retry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

log = logging.getLogger(__name__)

_T = TypeVar("_T")

# Таймаут одной попытки — kwarg request_timeout= у конкретного вызова
# (bot.get_me(request_timeout=...) и т.п.), не трогает bot.session.timeout
# (дефолт aiogram 60с) — обычный, не-стартовый трафик бота остаётся как был.
REQUEST_TIMEOUT_S = 10.0
DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY_S = 5.0


async def call_with_network_retry(
    call: Callable[[], Awaitable[_T]],
    *,
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
    delay_s: float = DEFAULT_DELAY_S,
) -> _T:
    """Повторить ``call`` при транзиентных сетевых сбоях Telegram.

    Ретраится только TelegramNetworkError (сюда aiogram заворачивает и
    asyncio.TimeoutError, и aiohttp.ClientError — ровно сценарий "DNS ок,
    TCP/HTTP ещё нет") и TelegramRetryAfter (429). Любая другая ошибка
    (bad token, forbidden, chat not found) пробрасывается сразу без единой
    лишней попытки — это не транзиентные ошибки, ретраить бессмысленно.

    Бюджет попыток намеренно небольшой (по умолчанию 3×10с таймаут +
    2×5с пауза = 40с): держится под SUPERIOR_READY_TIMEOUT_S=45с из
    node/lease.py, чтобы долгий локальный ретрай на старте не столкнулся с
    решением аренды забрать слот обратно. После исчерпания попыток
    исключение пробрасывается — процесс падает намеренно, длинные простои
    закрывает restart-loop супервизора (node/supervisor.py), не бесконечный
    ретрай здесь.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            log.warning(
                "%s: Telegram просит подождать %sс (попытка %d/%d)",
                what, exc.retry_after, attempt, attempts,
            )
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramNetworkError as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            log.warning(
                "%s: сетевая ошибка (%s) — повтор через %.0fс (попытка %d/%d)",
                what, exc, delay_s, attempt, attempts,
            )
            await asyncio.sleep(delay_s)
    assert last_exc is not None  # цикл всегда либо вернул результат, либо это выставил
    raise last_exc
