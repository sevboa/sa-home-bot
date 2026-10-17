from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from sa_home_bot.config import LlmConfig


class LazyModel:
    def __init__(self, load_sync: Callable[[LlmConfig], Any]) -> None:
        self._load_sync = load_sync
        self._value: Any = None
        self._lock = asyncio.Lock()

    async def get(self, cfg: LlmConfig) -> Any:
        if self._value is None:
            async with self._lock:
                if self._value is None:
                    self._value = await asyncio.to_thread(self._load_sync, cfg)
        return self._value
