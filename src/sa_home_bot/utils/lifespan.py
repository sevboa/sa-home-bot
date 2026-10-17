from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable

from sa_home_bot.utils.shutdown import ShutdownBudget

log = logging.getLogger(__name__)

ShutdownCallback = Callable[[], Awaitable[None]]

CALLBACK_TIMEOUT_S = 10.0


class Lifespan:
    def __init__(self) -> None:
        self._callbacks: list[ShutdownCallback] = []
        self._stop = asyncio.Event()

    def push(self, callback: ShutdownCallback) -> None:
        self._callbacks.append(callback)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        sigbreak = getattr(signal, "SIGBREAK", None)
        signals = [signal.SIGINT, signal.SIGTERM] + ([sigbreak] if sigbreak else [])
        for sig in signals:
            try:
                loop.add_signal_handler(sig, self.trigger)
            except NotImplementedError:
                signal.signal(sig, lambda *_: self.trigger())

    def trigger(self) -> None:
        log.info("Получен сигнал остановки")
        self._stop.set()

    async def wait(self) -> None:
        await self._stop.wait()

    async def shutdown(self) -> None:
        budget = ShutdownBudget(total_s=CALLBACK_TIMEOUT_S * len(self._callbacks) + 1)
        for callback in reversed(self._callbacks):
            name = getattr(callback, "__qualname__", repr(callback))
            await budget.step(name, callback(), timeout=CALLBACK_TIMEOUT_S)


async def cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
