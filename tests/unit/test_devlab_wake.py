from __future__ import annotations

from datetime import timedelta

import pytest

from sa_home_bot import wake_core
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import store as store_module
from sa_home_bot.devlab.hub import waker as waker_module
from sa_home_bot.devlab.hub.store import HubStore, Principal
from sa_home_bot.devlab.hub.waker import WAKE_BACKOFF_S, ExecutorWaker
from sa_home_bot.utils.clock import utc_now
from tests.unit.support.fakes import Clock

CLIENT = "claude-laptop"
EXECUTOR = "mycraft"
OTHER_EXECUTOR = "other"
MODEL = "qwen-local"
PROJECT = "demo"
S = protocol


@pytest.fixture
async def store(tmp_path):
    hub_store = HubStore(tmp_path)
    await hub_store.open()
    await hub_store.queue.add_project(PROJECT)
    yield hub_store
    await hub_store.close()


async def submit(store):
    return await store.queue.submit(CLIENT, PROJECT, MODEL, "# цель")


async def running(store):
    job = await submit(store)
    await store.queue.claim(EXECUTOR, [MODEL])
    return job


async def waiting(store):
    job = await running(store)
    await store.queue.post_from_executor(job.id, S.MSG_QUESTION, executor=EXECUTOR)
    return job


async def scenario_empty(store):
    return None


async def scenario_queued(store):
    await submit(store)


async def scenario_running(store):
    await running(store)


async def scenario_waiting(store):
    await waiting(store)


async def scenario_answered(store):
    job = await waiting(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_ANSWER, {})


async def scenario_answer_read(store):
    await scenario_answered(store)
    await store.queue.poll(executor=EXECUTOR)


async def scenario_released(store):
    job = await waiting(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE, {})


async def scenario_resumed(store):
    job = await waiting(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_RESUME, {})


async def scenario_stop_unassigned(store):
    job = await submit(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_STOP, {})


async def scenario_stop_running(store):
    job = await running(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_STOP, {})


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (scenario_empty, False),
        (scenario_queued, True),
        (scenario_running, False),
        (scenario_waiting, False),
        (scenario_answered, True),
        (scenario_answer_read, False),
        (scenario_released, False),
        (scenario_resumed, True),
        (scenario_stop_unassigned, False),
        (scenario_stop_running, True),
    ],
)
async def test_pending_work(store, scenario, expected):
    await scenario(store)
    assert await store.queue.pending_work() is expected


class FakeEnsure:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.outcome = wake_core.READY

    async def __call__(self, node_link, wake_store, node_id, service):
        self.calls.append((node_id, service))
        return self.outcome


@pytest.fixture
def ensure(monkeypatch):
    fake = FakeEnsure()
    monkeypatch.setattr(waker_module.wake_core, "ensure_service_ready", fake)
    return fake


@pytest.fixture
def clock():
    return Clock(1000.0)


@pytest.fixture
def waker(store, clock):
    return ExecutorWaker(store, object(), object(), EXECUTOR, clock)


async def beat(store, name=EXECUTOR):
    await store.heartbeat(Principal(name, S.ROLE_EXECUTOR), {S.FIELD_BUSY: False})


async def test_wakes_on_work_and_silence(store, waker, ensure):
    await submit(store)
    assert await waker.step() == wake_core.READY
    assert ensure.calls == [(EXECUTOR, S.EXECUTOR_SERVICE)]


async def test_fresh_heartbeat_does_not_wake(store, waker, ensure):
    await submit(store)
    await beat(store)
    assert await waker.step() is None
    assert ensure.calls == []


async def test_fresh_heartbeat_under_other_token_name_does_not_wake(store, waker, ensure):
    await submit(store)
    await beat(store, OTHER_EXECUTOR)
    assert await waker.step() is None
    assert ensure.calls == []


async def test_stale_heartbeat_wakes(store, waker, ensure, monkeypatch):
    await submit(store)
    await beat(store)
    later = utc_now() + timedelta(seconds=store_module.HEARTBEAT_FRESH_S + 1)
    monkeypatch.setattr(store_module, "utc_now", lambda: later)
    await waker.step()
    assert len(ensure.calls) == 1


async def test_no_work_does_not_wake(waker, ensure):
    assert await waker.step() is None
    assert ensure.calls == []


async def test_backoff(store, waker, ensure, clock):
    await submit(store)
    await waker.step()
    clock.advance(WAKE_BACKOFF_S - 1)
    assert await waker.step() is None
    clock.advance(1)
    await waker.step()
    assert len(ensure.calls) == 2


async def test_failed_attempt_also_backs_off(store, waker, ensure, clock):
    ensure.outcome = wake_core.UNREACHABLE
    await submit(store)
    assert await waker.step() == wake_core.UNREACHABLE
    clock.advance(10)
    assert await waker.step() is None
    assert len(ensure.calls) == 1


async def test_release_does_not_wake(store, waker, ensure):
    await scenario_released(store)
    assert await waker.step() is None
    assert ensure.calls == []


async def test_answer_after_release_wakes(store, waker, ensure):
    job = await waiting(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE, {})
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_ANSWER, {})
    await waker.step()
    assert len(ensure.calls) == 1
