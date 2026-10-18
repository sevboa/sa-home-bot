from __future__ import annotations

from sa_home_bot.bot import voice_mode


async def test_disabled_by_default(store):
    assert await voice_mode.is_enabled(store, 123) is False


async def test_enable_and_disable(store):
    await voice_mode.set_enabled(store, 123, True)
    assert await voice_mode.is_enabled(store, 123) is True

    await voice_mode.set_enabled(store, 123, False)
    assert await voice_mode.is_enabled(store, 123) is False


async def test_per_chat_isolation(store):
    await voice_mode.set_enabled(store, 1, True)
    assert await voice_mode.is_enabled(store, 1) is True
    assert await voice_mode.is_enabled(store, 2) is False
