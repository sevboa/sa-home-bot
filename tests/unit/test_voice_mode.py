"""Тумблер голосового режима (bot/voice_mode.py) — простой строковый флаг
в app_state, per-chat."""

from __future__ import annotations

import pytest_asyncio

from sa_home_bot.bot import voice_mode
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


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
