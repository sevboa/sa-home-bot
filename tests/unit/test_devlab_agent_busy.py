from __future__ import annotations

import json

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.agent_worker import AGENT_STATE_FILE, OUTBOX_FILE
from sa_home_bot.devlab.executor.busy import BusyTracker
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import ScriptedChatModel
from tests.unit.test_devlab_agent_owner import ASK, FINISH, WRITE_DONE, make_worker, submit
from tests.unit.test_devlab_agent_worker import FlakyHub

env = executor_tests.env
OWNER_WAIT_S = 100.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


def tracker_of(clock) -> BusyTracker:
    return BusyTracker(OWNER_WAIT_S, clock)


async def parked(env, clock, script=(WRITE_DONE, ASK)):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(*script), busy=tracker_of(clock))
    assert await worker.claim_once()
    return job_id, worker


def state_of(env) -> dict:
    return json.loads((env.tmp_path / "exec" / AGENT_STATE_FILE).read_text())


async def test_idle_before_and_after_a_finished_job(env, clock):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH), busy=tracker_of(clock))
    assert not worker._busy.busy()
    assert await worker.claim_once()
    assert await env.status(job_id) == protocol.STATUS_DONE
    assert not worker._busy.busy()


async def test_waiting_owner_holds_within_owner_wait_and_not_after(env, clock):
    job_id, worker = await parked(env, clock)
    assert worker._busy.holding_for_owner() == [job_id]
    assert worker._busy.busy()
    clock.now += OWNER_WAIT_S - 1
    assert worker._busy.busy()
    clock.now += 1
    assert not worker._busy.busy()


async def test_release_stops_holding_and_job_stays_parked(env, clock):
    job_id, worker = await parked(env, clock)
    await env.mcp.call(protocol.TOOL_RELEASE, id=job_id)
    await worker.poll_once()
    assert not worker._busy.busy()
    assert worker.parked_jobs == [job_id]
    assert state_of(env)["parked"][job_id]["released"] is True


async def test_released_job_stays_free_after_restart(env, clock):
    job_id, worker = await parked(env, clock)
    await env.mcp.call(protocol.TOOL_RELEASE, id=job_id)
    await worker.poll_once()
    restarted = make_worker(env, ScriptedChatModel(), busy=tracker_of(clock))
    assert not restarted._busy.busy()


async def test_question_time_survives_restart(env, clock):
    job_id, worker = await parked(env, clock)
    clock.now += OWNER_WAIT_S - 1
    restarted = make_worker(env, ScriptedChatModel(), busy=tracker_of(clock))
    assert restarted._busy.holding_for_owner() == [job_id]
    clock.now += 1
    assert not restarted._busy.busy()


async def test_answer_returns_to_running_hold_until_finished(env, clock):
    job_id, worker = await parked(env, clock)
    clock.now += OWNER_WAIT_S
    assert not worker._busy.busy()
    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="json")
    await worker.poll_once()
    assert worker._busy.running_jobs() == [job_id]
    assert worker._busy.busy()
    worker._models = lambda name, jid: ScriptedChatModel(FINISH)
    assert await worker.claim_once()
    assert not worker._busy.busy()


async def test_answered_job_is_running_after_restart(env, clock):
    job_id, worker = await parked(env, clock)
    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="json")
    await worker.poll_once()
    restarted = make_worker(env, ScriptedChatModel(FINISH), busy=tracker_of(clock))
    assert restarted._busy.running_jobs() == [job_id]


@pytest.mark.parametrize("command", [protocol.TOOL_STOP, protocol.TOOL_RESUME])
async def test_stop_and_resume_free_the_machine(env, clock, command):
    job_id, worker = await parked(env, clock)
    await env.mcp.call(command, id=job_id)
    await worker.poll_once()
    assert not worker._busy.busy()


async def test_stop_of_answered_job_clears_running(env, clock):
    job_id, worker = await parked(env, clock)
    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="json")
    await worker.poll_once()
    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await worker.poll_once()
    assert not worker._busy.busy()


async def test_undelivered_outbox_holds_until_drained(env, clock):
    job_id = await submit(env)
    busy = tracker_of(clock)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH), hub_class=FlakyHub, busy=busy)
    worker._hub.down = True
    assert await worker.claim_once()
    assert (env.tmp_path / "exec" / OUTBOX_FILE).exists()
    assert worker.pending_messages
    assert busy.busy()
    assert busy.running_jobs() == []

    worker._hub.down = False
    await worker.drain()
    assert not busy.busy()
    assert await env.status(job_id) == protocol.STATUS_DONE


async def test_hub_queue_from_heartbeat_holds(env, clock):
    worker = make_worker(env, ScriptedChatModel(), busy=tracker_of(clock))
    await worker.beat()
    assert not worker._busy.busy()
    await submit(env)
    await worker.beat()
    assert worker._busy.busy()
