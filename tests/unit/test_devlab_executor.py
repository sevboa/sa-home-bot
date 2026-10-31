from __future__ import annotations

import asyncio
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor import mirror as mirror_module
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.mirror import GitError, ProjectMirror, remote_url
from sa_home_bot.devlab.executor.runner import JUNIT_FILE
from sa_home_bot.devlab.executor.worker import (
    JOBS_DIR,
    STATE_FILE,
    TIMEOUT_EXIT_CODE,
    ExecutorState,
    TestWorker,
    WorkerSettings,
)
from sa_home_bot.devlab.hub import repos
from sa_home_bot.devlab.hub.app import build_hub_app
from sa_home_bot.devlab.hub.store import HubStore
from tests.unit.support.fakes import FakeNodes, FakeTestRunner
from tests.unit.test_devlab_e2e import Mcp
from tests.unit.test_devlab_git import arun_git, auth, run_git

PROJECT = "demo"
CLIENT = "laptop"
SPEC = b'[test]\ncmd = "python -m pytest -q"\ntimeout_s = 60\n'
PASSED = b"3 passed in 0.10s\n"


class Env:
    def __init__(self, client: TestClient, store: HubStore, tmp_path, tokens) -> None:
        self.client = client
        self.store = store
        self.tmp_path = tmp_path
        self.client_token, self.executor_token = tokens
        self.mcp = Mcp(client, self.client_token)
        self.hubs: list[ExecutorHub] = []
        self.git_url = ""

    @property
    def hub_url(self) -> str:
        return str(self.client.make_url("")).rstrip("/")

    async def start(self) -> None:
        added = await self.mcp.call(protocol.TOOL_PROJECT_ADD, name=PROJECT)
        self.git_url = str(self.client.make_url(added["git_path"]))

    async def push(self, branch: str, files: dict[str, bytes]) -> str:
        work = self.tmp_path / f"snap-{branch.replace('/', '-')}"
        work.mkdir()
        run_git(work, "init", "-q", "-b", "main")
        for name, data in files.items():
            (work / name).write_bytes(data)
        run_git(work, "add", "-A")
        run_git(work, "commit", "-q", "-m", "snap")
        pushed = await arun_git(
            work, *auth(self.client_token), "push", self.git_url, f"main:refs/heads/{branch}"
        )
        assert pushed.returncode == 0, pushed.stderr
        return run_git(work, "rev-parse", "HEAD").stdout.strip()

    async def submit(self, ref: str, **kwargs) -> str:
        data = await self.mcp.call(protocol.TOOL_TEST, project=PROJECT, ref=ref, **kwargs)
        return data["job"]["id"]

    def worker(self, runner: FakeTestRunner, name: str = "exec") -> TestWorker:
        hub = ExecutorHub(self.hub_url, self.executor_token)
        self.hubs.append(hub)
        return TestWorker(
            hub,
            runner,
            WorkerSettings(
                hub_url=self.hub_url,
                token=self.executor_token,
                work_dir=self.tmp_path / name,
                poll_interval_s=0.01,
                heartbeat_interval_s=0.01,
            ),
        )

    async def status(self, job_id: str) -> str:
        return (await self.store.queue.get_job(job_id)).status

    async def inbox_kinds(self) -> list[str]:
        inbox = await self.mcp.call(protocol.TOOL_INBOX)
        return [m["kind"] for m in inbox[protocol.FIELD_MESSAGES]]


@pytest.fixture
async def env(tmp_path):
    (tmp_path / "hub").mkdir()
    store = HubStore(tmp_path / "hub")
    await store.open()
    tokens = (
        await store.issue_token(CLIENT, protocol.ROLE_CLIENT),
        await store.issue_token("pc", protocol.ROLE_EXECUTOR),
    )
    async with TestClient(TestServer(build_hub_app(store, FakeNodes()))) as client:
        env = Env(client, store, tmp_path, tokens)
        await env.start()
        yield env
        for hub in env.hubs:
            await hub.close()
    await store.close()


async def test_full_test_job_path(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/1"
    await env.push(ref, {".devlab.toml": SPEC, "a.txt": b"x"})
    runner = FakeTestRunner(output=PASSED, junit=None)
    worker = env.worker(runner)

    waiting = asyncio.create_task(
        env.mcp.call(protocol.TOOL_TEST, project=PROJECT, ref=ref, wait=30)
    )
    async with asyncio.timeout(20):
        while not await worker.claim_once():
            await asyncio.sleep(0.02)
    data = await waiting

    result = data["result"]
    assert result[protocol.PAYLOAD_EXIT_CODE] == 0
    assert result[protocol.PAYLOAD_SUMMARY] == {
        protocol.SUMMARY_PASSED: 3,
        protocol.SUMMARY_FAILED: 0,
        protocol.SUMMARY_ERRORS: 0,
        protocol.SUMMARY_SKIPPED: 0,
    }
    assert result[protocol.PAYLOAD_TAIL] == PASSED.decode()
    (request,) = runner.requests
    assert request.argv == ("python", "-m", "pytest", "-q", f"--junitxml={JUNIT_FILE}")
    assert request.timeout_s == 60
    assert {"a.txt", ".devlab.toml"} <= set(runner.workdir_files)
    assert not (env.tmp_path / "exec" / JOBS_DIR / data["job"]["id"]).exists()

    log = await env.mcp.call(protocol.TOOL_TEST_LOG, id=data["job"]["id"])
    assert log["text"] == PASSED.decode()
    assert not await repos.branch_exists(env.store.git_root, PROJECT, ref)
    assert await env.status(data["job"]["id"]) == protocol.STATUS_DONE


async def test_junit_and_job_overrides(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/2"
    await env.push(ref, {".devlab.toml": SPEC})
    junit = (
        b'<testsuite><testcase classname="t.a" name="ok"/>'
        b'<testcase classname="t.a" name="bad"><failure message="boom"/></testcase></testsuite>'
    )
    runner = FakeTestRunner(output=b"garbage", exit_code=1, junit=junit)
    job_id = await env.submit(ref, cmd="pytest -q", args=["-k", "x"])
    worker = env.worker(runner)
    assert await worker.claim_once()

    (request,) = runner.requests
    assert request.argv == ("pytest", "-q", "-k", "x", f"--junitxml={JUNIT_FILE}")
    message = await env.store.queue.last_message(job_id, protocol.MSG_TEST_DONE)
    payload = message.payload
    assert payload[protocol.PAYLOAD_EXIT_CODE] == 1
    assert payload[protocol.PAYLOAD_SUMMARY][protocol.SUMMARY_FAILED] == 1
    assert payload[protocol.PAYLOAD_SUMMARY][protocol.SUMMARY_PASSED] == 1
    assert payload[protocol.PAYLOAD_FAILURES] == [
        {protocol.FAILURE_NAME: "t.a::bad", protocol.FAILURE_EXCERPT: "boom"}
    ]
    uploaded = await env.mcp.call(protocol.TOOL_TEST_LOG, id=job_id, file=protocol.LOG_FILE_JUNIT)
    assert "testcase" in uploaded["text"]


async def test_timeout_is_reported_as_exit_124(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/3"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    worker = env.worker(FakeTestRunner(output=b"slow", exit_code=-9, timed_out=True))
    assert await worker.claim_once()
    payload = (await env.store.queue.last_message(job_id, protocol.MSG_TEST_DONE)).payload
    assert payload[protocol.PAYLOAD_EXIT_CODE] == TIMEOUT_EXIT_CODE
    assert payload[protocol.PAYLOAD_TAIL].endswith("slow")


async def test_non_test_ref_is_not_deleted(env):
    await env.push(f"{protocol.BASE_BRANCH_PREFIX}x", {".devlab.toml": SPEC})
    job_id = await env.submit(f"{protocol.BASE_BRANCH_PREFIX}x")
    assert await env.worker(FakeTestRunner(output=PASSED)).claim_once()
    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await repos.branch_exists(env.store.git_root, PROJECT, f"{protocol.BASE_BRANCH_PREFIX}x")


@pytest.mark.parametrize(
    ("spec", "error", "expected"),
    [
        (SPEC, RuntimeError("boom"), "boom"),
        (b"", None, "не задана команда"),
        (b"[test", None, ".devlab.toml"),
    ],
)
async def test_executor_errors_become_failed_and_loop_survives(env, spec, error, expected):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/4"
    await env.push(ref, {".devlab.toml": spec} if spec else {"a.txt": b"x"})
    job_id = await env.submit(ref)
    worker = env.worker(FakeTestRunner(error=error))
    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert expected in failed.payload[protocol.PAYLOAD_TEXT]
    assert not await repos.branch_exists(env.store.git_root, PROJECT, ref)

    ref2 = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/5"
    await env.push(ref2, {".devlab.toml": SPEC})
    job2 = await env.submit(ref2)
    assert await env.worker(FakeTestRunner(output=PASSED)).claim_once()
    assert await env.status(job2) == protocol.STATUS_DONE


async def test_unknown_ref_fails_job(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/6"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    run_git(
        repos.repo_path(env.store.git_root, PROJECT),
        "update-ref",
        "-d",
        f"{protocol.HEADS_REF_PREFIX}{ref}",
    )
    assert await env.worker(FakeTestRunner()).claim_once()
    assert await env.status(job_id) == protocol.STATUS_FAILED


async def test_stop_cancels_running_job(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/7"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    runner = FakeTestRunner(block=True)
    worker = env.worker(runner)

    running = asyncio.create_task(worker.claim_once())
    async with asyncio.timeout(20):
        await runner.started.wait()
    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await worker.poll_once()
    assert await running

    assert runner.cancelled == [job_id]
    assert await env.status(job_id) == protocol.STATUS_STOPPED
    kinds = await env.inbox_kinds()
    assert protocol.MSG_STOPPED in kinds
    assert protocol.MSG_TEST_DONE not in kinds
    assert not await repos.branch_exists(env.store.git_root, PROJECT, ref)


async def test_cursor_survives_restart(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/8"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    runner = FakeTestRunner(block=True)
    first = env.worker(runner)
    running = asyncio.create_task(first.claim_once())
    async with asyncio.timeout(20):
        await runner.started.wait()
    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await first.poll_once()
    await running
    assert first.cursor > 0

    second = env.worker(FakeTestRunner())
    assert second.cursor == first.cursor
    messages, cursor = await second._hub.poll(second.cursor)
    assert messages == []
    assert cursor == first.cursor


async def test_restart_closes_orphaned_job(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/9"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    worker = env.worker(FakeTestRunner())
    claimed = await worker._hub.claim([protocol.JOB_KIND_TEST])
    assert claimed.id == job_id
    state_path = env.tmp_path / "exec" / STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(ExecutorState(current=job_id).model_dump_json())

    restarted = env.worker(FakeTestRunner())
    await restarted.recover()
    assert await env.status(job_id) == protocol.STATUS_FAILED
    assert json.loads(state_path.read_text())["current"] is None


async def test_heartbeat_reports_busy(env):
    worker = env.worker(FakeTestRunner())
    await worker.beat()
    assert await env.store.executor_alive()
    row = await env.store._db.conn.execute("SELECT data FROM heartbeats")
    assert json.loads((await row.fetchone())["data"]) == {
        protocol.FIELD_BUSY: False,
        protocol.FIELD_JOBS: [],
    }


async def test_run_loop_picks_up_job(env):
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/10"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    worker = env.worker(FakeTestRunner(output=PASSED))
    loop = asyncio.create_task(worker.run())
    try:
        async with asyncio.timeout(30):
            while await env.status(job_id) != protocol.STATUS_DONE:
                await asyncio.sleep(0.05)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert await env.store.executor_alive()


async def test_token_stays_out_of_git_argv_and_logs(env, monkeypatch, caplog):
    seen: list[tuple[list[str], dict[str, str]]] = []
    real_run = mirror_module.run

    async def spy(argv, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(mirror_module, "run", spy)
    caplog.set_level(logging.DEBUG)
    ref = f"{protocol.TEST_BRANCH_PREFIX}{CLIENT}/11"
    await env.push(ref, {".devlab.toml": SPEC})
    job_id = await env.submit(ref)
    assert await env.worker(FakeTestRunner(output=PASSED)).claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert len(seen) >= 4
    for argv, environ in seen:
        assert env.executor_token not in " ".join(argv)
        assert env.executor_token in environ["GIT_CONFIG_VALUE_0"]
    assert env.executor_token not in caplog.text


async def test_git_error_does_not_leak_token(env):
    bad = "dlh_" + "z" * 40
    mirror = ProjectMirror(env.tmp_path / "m.git", remote_url(env.hub_url, PROJECT), bad)
    with pytest.raises(GitError) as raised:
        await mirror.fetch()
    assert bad not in str(raised.value)
