from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store
from sa_home_bot.silence import GPU_SILENCE_KEY, SilenceService, SilenceState

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
INFINITE = "infinite"


def clock() -> datetime:
    return NOW


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "test.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def seed_silence(store: Store, state: SilenceState) -> None:
    await store.update_state(GPU_SILENCE_KEY, lambda raw: state.to_json())


@pytest.mark.parametrize(
    ("seed", "minutes", "expected_until", "expected_started_at"),
    [
        (None, 30, NOW + timedelta(minutes=30), NOW),
        (NOW + timedelta(minutes=60), 30, NOW + timedelta(minutes=60), NOW - timedelta(minutes=10)),
        (NOW + timedelta(minutes=60), 90, NOW + timedelta(minutes=90), NOW - timedelta(minutes=10)),
        (INFINITE, 30, None, NOW - timedelta(minutes=10)),
        (None, None, None, NOW),
    ],
)
async def test_start(store, seed, minutes, expected_until, expected_started_at):
    if isinstance(seed, datetime):
        await seed_silence(store, SilenceState(until=seed, started_at=NOW - timedelta(minutes=10)))
    elif seed == INFINITE:
        await seed_silence(store, SilenceState(until=None, started_at=NOW - timedelta(minutes=10)))
    state = await SilenceService(store, clock).start(minutes, set_by="me")
    assert state.until == expected_until
    assert state.started_at == expected_started_at
    stored = await SilenceService(store, clock).load()
    if expected_until is None:
        assert stored is not None and stored.until is None
    elif isinstance(seed, datetime):
        assert stored is not None and stored.until == expected_until


async def test_expired_silence_load_none_and_off(store):
    await seed_silence(
        store,
        SilenceState(until=NOW - timedelta(minutes=5), started_at=NOW - timedelta(minutes=60)),
    )
    service = SilenceService(store, clock)
    assert await service.load() is None
    assert await service.off() is True
    assert await service.off() is False


async def test_off_removes_active_silence(store):
    service = SilenceService(store, clock)
    await service.start(30)
    assert await service.load() is not None
    assert await service.off() is True
    assert await service.load() is None
    assert await service.off() is False


async def test_start_rejects_non_positive_minutes(store):
    with pytest.raises(ValueError):
        await SilenceService(store, clock).start(0)


async def test_from_json_broken_raw_is_none():
    assert SilenceState.from_json("{broken") is None
