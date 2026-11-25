
from __future__ import annotations

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.executor.agent_worker import AgentSettings, AgentWorker
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.worker import TestWorker, WorkerSettings
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import FakeTestRunner, ScriptedChatModel, StaticCatalog, local_entry
from tests.unit.test_devlab_agent_worker import BASE_FILES, FINISH

env = executor_tests.env
PROJECT = executor_tests.PROJECT
CLIENT = executor_tests.CLIENT


def park(active: bool):
    from sa_home_bot.devlab.executor.park import ParkMode

    return ParkMode(active=active)


def test_park_mode_holds_active_flag():
    from sa_home_bot.devlab.executor.park import ParkMode

    assert ParkMode(active=True).active
    assert not ParkMode(active=False).active


def test_parked_busy_counts_only_running_and_outbox():
    tracker = BusyTracker()
    tracker.set_parked(True)
    tracker.set_queued(2)
    tracker.waiting_owner("a")
    assert not tracker.busy()
    tracker.started("b")
    assert tracker.busy()
    tracker.finished("b")
    assert not tracker.busy()
    tracker.outbox_size = lambda: 1
    assert tracker.busy()
    tracker.outbox_size = lambda: 0
    assert not tracker.busy()


def test_unparked_busy_keeps_queue_and_owner():
    tracker = BusyTracker()
    tracker.set_queued(2)
    assert tracker.busy()
    tracker.set_queued(0)
    assert not tracker.busy()
    tracker.waiting_owner("a")
    assert tracker.busy()
    tracker.released("a")
    assert not tracker.busy()


def test_park_flag_can_be_removed():
    tracker = BusyTracker()
    tracker.set_queued(2)
    tracker.set_parked(True)
    assert not tracker.busy()
    tracker.set_parked(False)
    assert tracker.busy()


def test_worker_settings_carry_park(tmp_path):
    settings = WorkerSettings(
        hub_url="http://h",
        token="t",
        work_dir=tmp_path,
        poll_interval_s=1.0,
        heartbeat_interval_s=1.0,
        park=park(True),
    )
    assert settings.park == park(True)
    agent = AgentSettings(
        hub_url="http://h",
        token="t",
        work_dir=tmp_path,
        poll_interval_s=1.0,
        heartbeat_interval_s=1.0,
        park=park(False),
    )
    assert agent.park == park(False)


def make_test_worker(env, runner, active: bool = True) -> TestWorker:
    hub = ExecutorHub(env.hub_url, env.executor_token)
    env.hubs.append(hub)
    settings = WorkerSettings(
        hub_url=env.hub_url,
        token=env.executor_token,
        work_dir=env.tmp_path / "park-exec",
        poll_interval_s=0.01,
        heartbeat_interval_s=0.01,
        park=park(active),
    )
    return TestWorker(hub, runner, settings)


def make_agent_worker(env, model, active: bool = True) -> AgentWorker:
    hub = ExecutorHub(env.hub_url, env.executor_token)
    env.hubs.append(hub)
    settings = AgentSettings(
        hub_url=env.hub_url,
        token=env.executor_token,
        work_dir=env.tmp_path / "park-agent",
        poll_interval_s=0.01,
        heartbeat_interval_s=0.01,
        push_interval_s=0.0,
        retry_interval_s=0.01,
        park=park(active),
    )
    return AgentWorker(
        hub,
        lambda model_name, job_id, style="cb_v3", reminder=None: model,
        lambda request: LocalSandbox(request.workdir, request.baseline),
        settings,
        StaticCatalog(local_entry("flint")),
    )


def spy_claim(worker):
    seen = []

    async def spy(*args, **kwargs):
        seen.append(args)
        return None

    worker._hub.claim = spy
    return seen


async def submit_agent(env, base):
    await env.push(base, BASE_FILES)
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=PROJECT,
        model="flint",
        task="# сделай out.txt",
        base_branch=base,
    )
    return data["job"]["id"]


async def test_parked_test_worker_claims_nothing(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/1"
    await env.push(ref, {".devlab.toml": executor_tests.SPEC})
    job_id = await env.submit(ref)
    worker = make_test_worker(env, FakeTestRunner(output=executor_tests.PASSED))
    seen = spy_claim(worker)
    assert not await worker.claim_once()
    assert seen == []
    assert await env.status(job_id) == protocol.STATUS_QUEUED


async def test_unparked_test_worker_claims_as_before(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/2"
    await env.push(ref, {".devlab.toml": executor_tests.SPEC})
    job_id = await env.submit(ref)
    worker = make_test_worker(env, FakeTestRunner(output=executor_tests.PASSED), active=False)
    assert await worker.claim_once()
    assert await env.status(job_id) == protocol.STATUS_DONE


async def test_parked_agent_worker_claims_nothing(env):
    base = "base/park-a"
    job_id = await submit_agent(env, base)
    worker = make_agent_worker(env, ScriptedChatModel(FINISH))
    seen = spy_claim(worker)
    assert not await worker.claim_once()
    assert seen == []
    assert await env.status(job_id) == protocol.STATUS_QUEUED


async def test_unparked_agent_worker_claims_as_before(env):
    base = "base/park-b"
    job_id = await submit_agent(env, base)
    worker = make_agent_worker(env, ScriptedChatModel(FINISH), active=False)
    assert await worker.claim_once()
    assert await env.status(job_id) == protocol.STATUS_DONE
