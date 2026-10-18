from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

log = logging.getLogger(__name__)


class Lifespan:
    def __init__(self) -> None:
        self._stop = asyncio.Event()

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


async def cancel_task(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
