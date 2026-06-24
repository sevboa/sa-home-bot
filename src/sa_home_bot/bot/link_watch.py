"""Watchdog связи с Telegram как request-middleware.

Отслеживает сетевые сбои исходящих запросов; при восстановлении после долгого
дисконнекта вызывает on_reconnect (broadcast system-события).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from time import monotonic

from aiogram import Bot
from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import Response, TelegramType

log = logging.getLogger(__name__)

OnReconnect = Callable[[float], Awaitable[None]]


class LinkWatchMiddleware(BaseRequestMiddleware):
    def __init__(self, on_reconnect: OnReconnect, threshold_seconds: float = 60.0) -> None:
        self._on_reconnect = on_reconnect
        self._threshold = threshold_seconds
        self._down_since: float | None = None

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            result = await make_request(bot, method)
        except TelegramNetworkError:
            if self._down_since is None:
                self._down_since = monotonic()
                log.warning("Потеря связи с Telegram")
            raise
        else:
            if self._down_since is not None:
                downtime = monotonic() - self._down_since
                self._down_since = None
                log.info("Связь с Telegram восстановлена (офлайн %.0fс)", downtime)
                if downtime >= self._threshold:
                    asyncio.create_task(self._on_reconnect(downtime))
            return result
