"""Лимит ручного форс-скана: раз в минуту, 5 за скользящие сутки."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot.bot import scan_limit
from sa_home_bot.bot.scan_limit import MAX_PER_DAY
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC)


def test_first_scan_allowed():
    d = scan_limit.decide([], T0)
    assert d.allowed
    assert d.ticks == (T0,)


def test_blocked_within_a_minute():
    prev = [T0 - timedelta(seconds=30)]
    d = scan_limit.decide(prev, T0)
    assert not d.allowed
    assert "Слишком часто" in d.reason
    # Метку не добавили — только очищенный список.
    assert d.ticks == tuple(prev)


def test_allowed_after_a_minute():
    d = scan_limit.decide([T0 - timedelta(seconds=61)], T0)
    assert d.allowed
    assert len(d.ticks) == 2


def test_daily_limit_blocks_sixth():
    # 5 сканов за последние часы, последний — 2 мин назад (интервал ок).
    ticks = [T0 - timedelta(hours=h) for h in (10, 8, 6, 4, 2)]
    ticks[-1] = T0 - timedelta(minutes=2)
    d = scan_limit.decide(ticks, T0)
    assert not d.allowed
    assert "Лимит" in d.reason and str(MAX_PER_DAY) in d.reason


def test_old_ticks_outside_window_are_pruned():
    # 5 меток, но все старше суток → окно пустое, скан разрешён.
    ticks = [T0 - timedelta(days=1, hours=h) for h in range(5)]
    d = scan_limit.decide(ticks, T0)
    assert d.allowed
    assert d.ticks == (T0,)  # старые выкинуты, осталась только новая


def test_ticks_capped_at_max():
    ticks = [T0 - timedelta(minutes=m) for m in (50, 40, 30, 20, 10)]
    # последний 10 мин назад — интервал ок, но уже 5 в окне → блок
    d = scan_limit.decide(ticks, T0)
    assert not d.allowed


# --- store roundtrip ---


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "limit.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


async def test_store_action_ticks_roundtrip(store):
    assert await store.get_action_ticks("monitor:scan_now") == []
    ticks = [T0 - timedelta(minutes=5), T0]
    await store.set_action_ticks("monitor:scan_now", ticks)
    assert await store.get_action_ticks("monitor:scan_now") == ticks
    # Ключи независимы по действиям.
    assert await store.get_action_ticks("monitor:other") == []


async def test_store_action_ticks_bad_json(store):
    await store.set_state("action_ticks:monitor:scan_now", "не-json")
    assert await store.get_action_ticks("monitor:scan_now") == []


# --- actions.run_action: describe → команда, лимит, расход слота ---


class FakeLink:
    display_name = "монитор"

    def __init__(self, result=None, fail=False, action_ids=("scan_now",)):
        from sa_home_bot.proto.messages import ActionSpec

        self.calls: list[tuple[str, dict]] = []
        self.connected = not fail
        self._result = result if result is not None else {
            "sensor_queued": True,
            "smart_queued": True,
        }
        self._fail = fail
        self._actions = tuple(
            ActionSpec(id=a, title=f"Действие {a}") for a in action_ids
        )

    async def actions(self):
        return self._actions

    async def command(self, action, args=None):
        from sa_home_bot.bot.service_link import ServiceUnavailableError

        if self._fail:
            raise ServiceUnavailableError("нет связи")
        self.calls.append((action, args or {}))
        return self._result


async def test_run_action_sends_command_and_records_tick(store):
    from sa_home_bot.bot import actions

    link = FakeLink()
    text = await actions.run_action(store, link, "monitor", "scan_now")

    assert "Принято" in text
    assert link.calls == [("scan_now", {})]
    # Слот израсходован — записана одна метка.
    assert len(await store.get_action_ticks("monitor:scan_now")) == 1


async def test_run_action_blocked_when_too_soon(store):
    from sa_home_bot.bot import actions

    await store.set_action_ticks("monitor:scan_now", [datetime.now(tz=UTC)])
    link = FakeLink()
    text = await actions.run_action(store, link, "monitor", "scan_now")

    assert "Слишком часто" in text
    assert link.calls == []  # до монитора не дошло


async def test_run_action_service_down_keeps_slot(store):
    from sa_home_bot.bot import actions

    text = await actions.run_action(store, FakeLink(fail=True), "monitor", "scan_now")

    assert "недоступна" in text
    # Слот НЕ израсходован — действие не состоялось.
    assert await store.get_action_ticks("monitor:scan_now") == []


async def test_run_action_already_queued_keeps_slot(store):
    from sa_home_bot.bot import actions

    link = FakeLink(result={"sensor_queued": False, "smart_queued": False})
    text = await actions.run_action(store, link, "monitor", "scan_now")

    assert "Уже в очереди" in text
    assert await store.get_action_ticks("monitor:scan_now") == []


async def test_run_action_not_rate_limited_for_node(store):
    from sa_home_bot.bot import actions

    link = FakeLink(result={"service": {"name": "monitor"}}, action_ids=("restart",))
    # Два подряд — node-действия не лимитируются.
    await actions.run_action(store, link, "node", "restart", "monitor")
    text = await actions.run_action(store, link, "node", "restart", "monitor")
    assert "Принято" in text
    assert len(link.calls) == 2
    assert await store.get_action_ticks("node:restart") == []
