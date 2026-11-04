from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from sa_home_bot.utils.timefmt import local_day_start


def _make_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=UTC)


async def _insert_messages(
    store, chat_id: int, start_id: int, count: int, base_time: datetime, keep: int = 10000
):
    for i in range(count):
        msg_id = start_id + i
        await store.record_group_message(
            chat_id=chat_id,
            message_id=msg_id,
            created_at=base_time,
            user_id=100 + i,
            user_name=f"user{i}",
            content=f"msg{msg_id}",
            reply_to=None,
            from_alfred=False,
            keep=keep,
        )


@pytest.mark.asyncio
async def test_record_group_message_trim_keep(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    await _insert_messages(store, 2, 1000, 3, base, keep=1000)
    await _insert_messages(store, 1, 1, 1001, base, keep=1000)
    rows = await store.recent_group_messages(1, 10000)
    assert len(rows) == 1000
    assert rows[0]["message_id"] == 2
    assert rows[-1]["message_id"] == 1001
    rows2 = await store.recent_group_messages(2, 10000)
    assert len(rows2) == 3
    assert rows2[0]["message_id"] == 1000
    assert rows2[-1]["message_id"] == 1002


@pytest.mark.asyncio
async def test_record_group_message_upsert_updates_content(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    await store.record_group_message(
        chat_id=1,
        message_id=5,
        created_at=base,
        user_id=1,
        user_name="u",
        content="old",
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )
    await store.record_group_message(
        chat_id=1,
        message_id=5,
        created_at=base,
        user_id=1,
        user_name="u",
        content="new",
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )
    row = await store._fetch_one(
        "SELECT content FROM group_messages WHERE chat_id=1 AND message_id=5"
    )
    assert row["content"] == "new"
    count = await store._fetch_one("SELECT COUNT(*) as c FROM group_messages WHERE chat_id=1")
    assert count["c"] == 1


@pytest.mark.asyncio
async def test_edit_group_message(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    await store.record_group_message(
        chat_id=1,
        message_id=10,
        created_at=base,
        user_id=1,
        user_name="u",
        content="orig",
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )
    await store.edit_group_message(1, 10, "edited")
    row = await store._fetch_one(
        "SELECT content FROM group_messages WHERE chat_id=1 AND message_id=10"
    )
    assert row["content"] == "edited"
    await store.edit_group_message(1, 999, "nope")
    row = await store._fetch_one(
        "SELECT content FROM group_messages WHERE chat_id=1 AND message_id=999"
    )
    assert row is None


@pytest.mark.asyncio
async def test_delete_group_message(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    await store.record_group_message(
        chat_id=1,
        message_id=20,
        created_at=base,
        user_id=1,
        user_name="u",
        content="todelete",
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )
    await store.delete_group_message(1, 20)
    row = await store._fetch_one("SELECT * FROM group_messages WHERE chat_id=1 AND message_id=20")
    assert row is None


@pytest.mark.asyncio
async def test_recent_group_messages_order_and_limit(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    for i in range(10):
        await store.record_group_message(
            chat_id=1,
            message_id=100 + i,
            created_at=base,
            user_id=1,
            user_name="u",
            content=f"m{100+i}",
            reply_to=None,
            from_alfred=False,
            keep=1000,
        )
    rows = await store.recent_group_messages(1, 3)
    assert [r["message_id"] for r in rows] == [107, 108, 109]
    rows = await store.recent_group_messages(1, 2, before_message_id=105)
    assert [r["message_id"] for r in rows] == [103, 104]


@pytest.mark.asyncio
async def test_group_messages_since_time_and_id_bounds(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    for i in range(10):
        await store.record_group_message(
            chat_id=1,
            message_id=200 + i,
            created_at=base,
            user_id=1,
            user_name="u",
            content=f"m{200+i}",
            reply_to=None,
            from_alfred=False,
            keep=1000,
        )
    since = _make_dt("2026-06-15T11:59:00+00:00")
    rows = await store.group_messages_since(1, since)
    assert [r["message_id"] for r in rows] == list(range(200, 210))
    rows = await store.group_messages_since(1, since, after_message_id=203)
    assert [r["message_id"] for r in rows] == list(range(204, 210))
    rows = await store.group_messages_since(1, since, before_message_id=207)
    assert [r["message_id"] for r in rows] == list(range(200, 207))
    rows = await store.group_messages_since(1, since, after_message_id=203, before_message_id=207)
    assert [r["message_id"] for r in rows] == list(range(204, 207))


@pytest.mark.asyncio
async def test_purge_group_messages_except(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    await store.record_group_message(
        chat_id=1,
        message_id=1,
        created_at=base,
        user_id=1,
        user_name="u",
        content="c1",
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )
    await store.record_group_message(
        chat_id=2,
        message_id=1,
        created_at=base,
        user_id=1,
        user_name="u",
        content="c2",
        reply_to=None,
        from_alfred=False,
        keep=1000,
    )
    await store.save_group_day_summary(1, "2026-06-15", 1, "sum1", base)
    await store.save_group_day_summary(2, "2026-06-15", 1, "sum2", base)
    await store.purge_group_messages_except([1])
    rows1 = await store.recent_group_messages(1, 10)
    rows2 = await store.recent_group_messages(2, 10)
    assert len(rows1) == 1
    assert len(rows2) == 0
    sum1 = await store.group_day_summary(1)
    sum2 = await store.group_day_summary(2)
    assert sum1 is not None
    assert sum2 is None
    await store.purge_group_messages_except([])
    rows1 = await store.recent_group_messages(1, 10)
    assert len(rows1) == 0


@pytest.mark.asyncio
async def test_save_group_day_summary_upsert(store):
    base = _make_dt("2026-06-15T12:00:00+00:00")
    await store.save_group_day_summary(1, "2026-06-15", 100, "first", base)
    row = await store.group_day_summary(1)
    assert row["day"] == "2026-06-15"
    assert row["upto_message_id"] == 100
    assert row["summary"] == "first"
    await store.save_group_day_summary(1, "2026-06-16", 200, "second", base)
    row = await store.group_day_summary(1)
    assert row["day"] == "2026-06-16"
    assert row["upto_message_id"] == 200
    assert row["summary"] == "second"


def test_local_day_start():
    tz = ZoneInfo("Europe/Bucharest")
    assert local_day_start(datetime(2026, 3, 29, 12, 0, 0, tzinfo=UTC), tz) == datetime(
        2026, 3, 28, 22, 0, 0, tzinfo=UTC
    )
    assert local_day_start(datetime(2026, 10, 25, 12, 0, 0, tzinfo=UTC), tz) == datetime(
        2026, 10, 24, 21, 0, 0, tzinfo=UTC
    )
    assert local_day_start(datetime(2026, 6, 15, 22, 30, 0, tzinfo=UTC), tz) == datetime(
        2026, 6, 15, 21, 0, 0, tzinfo=UTC
    )
