from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sa_home_bot.devlab import protocol as S
from sa_home_bot.devlab.hub import mcp_tools
from sa_home_bot.devlab.hub.queue import ADD_ACTIVE_COLUMNS
from sa_home_bot.devlab.hub.store import HubStore

CLIENT = "claude-laptop"
EXECUTOR = "mycraft"
MODEL = "qwen-local"
PROJECT = "demo"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr("sa_home_bot.devlab.hub.queue.utc_now", fake)
    return fake


@pytest.fixture
async def store(tmp_path, clock):
    hub = HubStore(tmp_path)
    await hub.open()
    await hub.queue.add_project(PROJECT)
    yield hub
    await hub.close()


async def active(queue, job_id):
    return (await queue.get_job(job_id)).active_s()


async def test_sum_of_two_running_segments(store, clock):
    queue = store.queue
    job = await queue.submit(CLIENT, PROJECT, MODEL, "# цель\nсделать", 0)
    clock.advance(500)
    assert await active(queue, job.id) == 0
    await queue.claim(EXECUTOR, [MODEL])
    clock.advance(100)
    await queue.post_from_executor(job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
    clock.advance(1000)
    assert await active(queue, job.id) == 100
    await queue.post_from_client(CLIENT, job.id, S.MSG_ANSWER, {S.PAYLOAD_TEXT: "да"})
    clock.advance(30)
    await queue.post_from_executor(job.id, S.MSG_DONE)
    clock.advance(900)
    assert await active(queue, job.id) == 130


async def test_running_value_grows(store, clock):
    queue = store.queue
    job = await queue.submit(CLIENT, PROJECT, MODEL, "# цель\nсделать", 0)
    await queue.claim(EXECUTOR, [MODEL])
    clock.advance(60)
    assert await active(queue, job.id) == 60
    clock.advance(60)
    assert await active(queue, job.id) == 120
    view = mcp_tools.job_view(await queue.get_job(job.id))
    assert view[S.FIELD_ACTIVE_S] == 120
    assert "active_since" not in view


async def test_requeue_closes_segment(store, clock):
    queue = store.queue
    job = await queue.submit(CLIENT, PROJECT, MODEL, "# цель\nсделать", 0)
    await queue.claim(EXECUTOR, [MODEL])
    clock.advance(40)
    await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)
    clock.advance(500)
    assert await active(queue, job.id) == 40
    await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME)
    clock.advance(500)
    assert await active(queue, job.id) == 40


async def test_old_database_gets_columns(tmp_path, clock):
    first = HubStore(tmp_path)
    await first.open()
    for column in ("active_seconds", "active_since"):
        await first._db.conn.execute(f"ALTER TABLE jobs DROP COLUMN {column}")
    await first._db.conn.commit()
    await first.close()
    second = HubStore(tmp_path)
    await second.open()
    await second.queue.add_project(PROJECT)
    job = await second.queue.submit(CLIENT, PROJECT, MODEL, "# цель\nсделать", 0)
    assert (await second.queue.get_job(job.id)).active_seconds == 0
    assert len(ADD_ACTIVE_COLUMNS) == 2
    await second.close()
