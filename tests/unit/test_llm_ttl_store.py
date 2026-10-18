from __future__ import annotations

from datetime import datetime

from sa_home_bot.llm.ttl_store import TtlStore
from tests.unit.support.fakes import Clock


def test_sweep_evicts_only_entries_older_than_ttl():
    clock = Clock(datetime(2026, 1, 1))
    store = TtlStore[bytes](300.0, clock=clock)
    store.put("stale", b"old")
    clock.advance(seconds=301)
    store.put("fresh", b"new")
    clock.advance(seconds=1)

    store.sweep()

    assert list(store) == ["fresh"]


def test_untouched_entry_never_expires_until_touched():
    clock = Clock(datetime(2026, 1, 1))
    store = TtlStore[dict](120.0, clock=clock)
    store.put("running", {"done": False}, touched=False)
    clock.advance(seconds=10_000)
    store.sweep()
    assert "running" in store

    store.touch("running")
    clock.advance(seconds=121)
    store.sweep()
    assert "running" not in store


def test_touch_postpones_expiry_and_pop_returns_value():
    clock = Clock(datetime(2026, 1, 1))
    store = TtlStore[bytes](100.0, clock=clock)
    store.put("a", b"x")
    clock.advance(seconds=80)
    store.touch("a")
    clock.advance(seconds=80)
    store.sweep()
    assert store.get("a") == b"x"
    assert store.pop("a") == b"x"
    assert store.pop("a") is None
