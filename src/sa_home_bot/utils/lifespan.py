"""Lifespan — LIFO-стек shutdown-колбэков + обработка сигналов SIGINT/SIGTERM."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from sa_home_bot.utils.shutdown import ShutdownBudget

log = logging.getLogger(__name__)

ShutdownCallback = Callable[[], Awaitable[None]]

# Потолок на один shutdown-колбэк службы: закрыть БД, отписаться от поллинга,
# погасить сокет. Больше десяти секунд это не занимает ни при какой погоде, а
# зависшую службу всё равно добьёт супервизор ноды (stop_timeout_s).
CALLBACK_TIMEOUT_S = 10.0


class Lifespan:
    def __init__(self) -> None:
        self._callbacks: list[ShutdownCallback] = []
        self._stop = asyncio.Event()

    def push(self, callback: ShutdownCallback) -> None:
        """Зарегистрировать колбэк остановки (выполняются в обратном порядке)."""
        self._callbacks.append(callback)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        # SIGBREAK — только Windows: супервизор шлёт дочерней службе
        # CTRL_BREAK_EVENT (см. node/supervisor.py), питон видит его как SIGBREAK.
        sigbreak = getattr(signal, "SIGBREAK", None)
        signals = [signal.SIGINT, signal.SIGTERM] + ([sigbreak] if sigbreak else [])
        for sig in signals:
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
        # Каждый колбэк — под потолком (этап 29): служба, которая не может
        # закрыть свой сокет, не имеет права держать останов. Сорвавшийся
        # колбэк называется в логе, остальные выполняются как обычно.
        budget = ShutdownBudget(total_s=CALLBACK_TIMEOUT_S * len(self._callbacks) + 1)
        for callback in reversed(self._callbacks):
            name = getattr(callback, "__qualname__", repr(callback))
            await budget.step(name, callback(), timeout=CALLBACK_TIMEOUT_S)
