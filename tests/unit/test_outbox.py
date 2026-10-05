"""Этап 52: постоянная очередь исходящих (db/store.py outbox, bot/outbox.py,
постановка из Notifier.send_direct)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from sa_home_bot.bot import notifier as notifier_module
from sa_home_bot.bot.notifier import MAX_RETRIES, Notifier
from sa_home_bot.bot.outbox import flush_outbox
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import (
    OUTBOX_KIND_ALERT,
    OUTBOX_KIND_DELIVER,
    OUTBOX_KIND_INTERACTIVE,
    OUTBOX_KIND_TASK,
    Store,
)

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def store(tmp_path):
    db = Database(tmp_path / "t.sqlite")
    await db.open()
    await apply_migrations(db)
    yield Store(db)
    await db.close()


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.script: list[Exception | None] = []  # по одному на вызов

    async def send_message(self, chat_id, text, reply_parameters=None, reply_markup=None,
                           message_thread_id=None):
        if self.script:
            exc = self.script.pop(0)
            if exc is not None:
                raise exc
        self.sent.append((chat_id, text))

        class _M:
            message_id = 1

        return _M()


def _bad() -> TelegramBadRequest:
    return TelegramBadRequest(method=None, message="bad")  # type: ignore[arg-type]


async def _noop_sleep(_):
    return None


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(notifier_module.asyncio, "sleep", _noop_sleep)


async def test_ttl_by_kind(store):
    for kind in (OUTBOX_KIND_ALERT, OUTBOX_KIND_TASK, OUTBOX_KIND_DELIVER, OUTBOX_KIND_INTERACTIVE):
        await store.outbox_enqueue(1, kind, {"text": kind}, now=T0)
    assert len(await store.outbox_due(T0 + timedelta(minutes=14))) == 4
    assert {r["kind"] for r in await store.outbox_due(T0 + timedelta(minutes=16))} == {
        OUTBOX_KIND_ALERT, OUTBOX_KIND_TASK, OUTBOX_KIND_DELIVER}
    assert {r["kind"] for r in await store.outbox_due(T0 + timedelta(hours=7))} == {
        OUTBOX_KIND_TASK, OUTBOX_KIND_DELIVER}
    assert await store.outbox_due(T0 + timedelta(hours=25)) == []
    assert await store.outbox_expire(T0 + timedelta(hours=7)) == 2
    assert await store.outbox_count() == 2


async def test_dedup_replaces_old(store):
    await store.outbox_enqueue(1, OUTBOX_KIND_ALERT, {"text": "old"}, now=T0, dedup_key="n:cpu")
    await store.outbox_enqueue(2, OUTBOX_KIND_ALERT, {"text": "other"}, now=T0, dedup_key="n:cpu")
    await store.outbox_enqueue(1, OUTBOX_KIND_ALERT, {"text": "new"}, now=T0, dedup_key="n:cpu")
    rows = await store.outbox_due(T0)
    assert sorted(r["payload"]["text"] for r in rows) == ["new", "other"]


async def test_order_by_created_at(store):
    await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": "b"}, now=T0 + timedelta(seconds=5))
    await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": "a"}, now=T0)
    bot = FakeBot()
    n = await flush_outbox(Notifier(bot), store, now=T0 + timedelta(seconds=10))
    assert n == 2
    assert [t.split("\n")[0] for _, t in bot.sent] == ["a", "b"]
    assert await store.outbox_count() == 0


async def test_flush_429_waits_and_retries(store):
    await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": "x"}, now=T0)
    bot = FakeBot()
    bot.script = [TelegramRetryAfter(method=None, message="m", retry_after=3)]  # type: ignore[arg-type]
    slept: list[float] = []

    async def sl(s):
        slept.append(s)

    assert await flush_outbox(Notifier(bot), store, now=T0, sleep=sl) == 1
    assert slept == [4]
    assert bot.sent == [(1, "x")]


async def test_transient_stops_flush(store):
    for t in "abc":
        await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": t}, now=T0)
    bot = FakeBot()
    bot.script = [None, ConnectionError("down")]
    assert await flush_outbox(Notifier(bot), store, now=T0) == 1
    rows = await store.outbox_due(T0)
    assert [r["payload"]["text"] for r in rows] == ["b", "c"]
    assert [r["attempts"] for r in rows] == [1, 0]


async def test_permanent_deletes_and_continues(store):
    await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": "a"}, now=T0)
    await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": "b"}, now=T0)
    bot = FakeBot()
    bot.script = [TelegramForbiddenError(method=None, message="blocked")]  # type: ignore[arg-type]
    assert await flush_outbox(Notifier(bot), store, now=T0) == 1
    assert bot.sent == [(1, "b")]
    assert await store.outbox_count() == 0


async def test_late_mark(store):
    await store.outbox_enqueue(1, OUTBOX_KIND_TASK, {"text": "late"}, now=T0)
    await store.outbox_enqueue(
        1, OUTBOX_KIND_TASK, {"text": "fresh"}, now=T0 + timedelta(seconds=100)
    )
    bot = FakeBot()
    await flush_outbox(Notifier(bot), store, now=T0 + timedelta(seconds=110))
    hhmm = T0.astimezone().strftime("%H:%M")
    assert bot.sent[0][1] == f"late\n\n⏳ с задержкой, {hhmm}"
    assert bot.sent[1][1] == "fresh"  # 10 с — без пометки


async def test_notifier_enqueues_on_transient(store):
    bot = FakeBot()
    bot.script = [ConnectionError("x")] * MAX_RETRIES
    n = Notifier(bot, outbox=store)
    assert await n.send_direct(5, "hi", outbox_kind=OUTBOX_KIND_ALERT, dedup_key="k") is None
    rows = await store.outbox_due(datetime.now(UTC))
    assert len(rows) == 1 and rows[0]["payload"]["text"] == "hi"
    assert rows[0]["kind"] == OUTBOX_KIND_ALERT and rows[0]["dedup_key"] == "k"


async def test_notifier_permanent_not_enqueued(store):
    bot = FakeBot()
    bot.script = [_bad()]
    assert await Notifier(bot, outbox=store).send_direct(5, "hi") is None
    assert await store.outbox_count() == 0


async def test_notifier_without_outbox_old_behavior():
    bot = FakeBot()
    bot.script = [ConnectionError("x")] * MAX_RETRIES
    assert await Notifier(bot).send_direct(5, "hi") is None
    assert len(bot.script) == 0 and bot.sent == []
