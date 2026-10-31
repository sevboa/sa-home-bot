from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

MINUTE_S = 60.0

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RateWindow:
    def __init__(
        self, per_minute: int, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._per_minute <= 0:
            return
        async with self._lock:
            while True:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= MINUTE_S:
                    self._stamps.popleft()
                if len(self._stamps) < self._per_minute:
                    self._stamps.append(now)
                    return
                await self._sleep(self._stamps[0] + MINUTE_S - now)
