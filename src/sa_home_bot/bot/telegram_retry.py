from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter

log = logging.getLogger(__name__)

_T = TypeVar("_T")

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
                what,
                exc.retry_after,
                attempt,
                attempts,
            )
            await asyncio.sleep(exc.retry_after + 1)
        except TelegramNetworkError as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            log.warning(
                "%s: сетевая ошибка (%s) — повтор через %.0fс (попытка %d/%d)",
                what,
                exc,
                delay_s,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay_s)
    assert last_exc is not None
    raise last_exc
