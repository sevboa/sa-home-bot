from __future__ import annotations

import asyncio

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm.lazy_load import LazyModel


async def test_loads_once_under_concurrent_requests():
    calls = []

    def load(cfg):
        calls.append(cfg)
        return object()

    lazy = LazyModel(load)
    cfg = LlmConfig()
    first, second, third = await asyncio.gather(lazy.get(cfg), lazy.get(cfg), lazy.get(cfg))

    assert first is second is third
    assert len(calls) == 1
