"""Lifespan — LIFO-стек shutdown-колбэков + обработка сигналов SIGINT/SIGTERM."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

ShutdownCallback = Callable[[], Awaitable[None]]


class Lifespan:
    def __init__(self) -> None:
        self._callbacks: list[ShutdownCallback] = []
        self._stop = asyncio.Event()

    def push(self, callback: ShutdownCallback) -> None:
        """Зарегистрировать колбэк остановки (выполняются в обратном порядке)."""
        self._callbacks.append(callback)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.trigger)
            except NotImplementedError:  # напр. Windows
                signal.signal(sig, lambda *_: self.trigger())

    def trigger(self) -> None:
        log.info("Получен сигнал остановки")
        self._stop.set()

    async def wait(self) -> None:
        await self._stop.wait()

    async def shutdown(self) -> None:
        for callback in reversed(self._callbacks):
            try:
                await callback()
            except Exception:  # noqa: BLE001 — один сбойный колбэк не блокирует остальные
                log.exception("Ошибка в shutdown-колбэке")
