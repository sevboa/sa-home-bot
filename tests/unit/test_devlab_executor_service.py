from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.config import DevlabConfig, DevlabModelConfig, Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.executor import app as executor_app
from sa_home_bot.devlab.models.backends import build_backend
from sa_home_bot.devlab.models.gateway import Gateway
from sa_home_bot.devlab.models.hubclient import HubClient
from sa_home_bot.devlab.models.ledger import Ledger
from sa_home_bot.devlab.models.limits import RateWindow
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import FakeTestRunner
from tests.unit.support.waiting import wait_until
from tests.unit.test_devlab_agent_repo import hub_branch, tracked_files
from tests.unit.test_devlab_agent_worker import (
    BASE_FILES,
    MODEL,
    STARTED_DONE,
    agent_of,
    submit,
    wip_of,
)

env = executor_tests.env
GATEWAYS = ("gw", "jeeves")
TEST_REF = f"{protocol.TEST_BRANCH_PREFIX}laptop/svc"
TEST_SPEC = b'[test]\ncmd = "python -m pytest -q"\ntimeout_s = 60\n'
DONE_TIMEOUT_S = 30


def reply(name: str, call_id: str, **arguments) -> dict:
    call = {"id": call_id, "type": "function", "function": {"name": name}}
    call["function"]["arguments"] = json.dumps(arguments)
    return {
        "choices": [{"message": {"content": "", "tool_calls": [call]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10},
    }


class Upstream:
    def __init__(self) -> None:
        self.script = [
            reply("write_file", "w1", path="out.txt", content="done\n"),
            reply("finish", "f1", summary="сделано"),
        ]
        self.gate = asyncio.Event()
        self.gate.set()
        self.requests = 0

    async def chat(self, request: web.Request) -> web.Response:
        self.requests += 1
        await request.json()
        await self.gate.wait()
        return web.json_response(self.script.pop(0))


class Rig:
    def __init__(self, env, upstream: Upstream) -> None:
        self.env = env
        self.upstream = upstream
        self.servers: dict[str, TestServer] = {}
        self.gateways: dict[str, Gateway] = {}
        self.stop = asyncio.Event()
        self.executor: asyncio.Task | None = None
        self.sandbox_hook = None

    async def add_gateway(self, name: str, upstream_server: TestServer) -> None:
        env = self.env
        spec = DevlabModelConfig(
            base_url=str(upstream_server.make_url("/v1")),
            model="upstream-model",
            free=True,
            remote=False,
            context=8192,
        )
        ledger = Ledger(env.tmp_path / f"ledger-{name}")
        await ledger.open()
        token = await env.store.issue_token(name, protocol.ROLE_GATEWAY)
        hub = HubClient(env.hub_url, token)
        backend = build_backend(MODEL, spec)
        gateway = Gateway({MODEL: backend}, ledger, hub, {MODEL: RateWindow(0)})
        server = TestServer(gateway.build_app())
        await server.start_server()
        gateway.url = str(server.make_url("")).rstrip("/")
        await hub.heartbeat(await gateway.heartbeat_body())
        self.servers[name] = server
        self.gateways[name] = gateway
        env.hubs.append(hub)

    def sandboxes(self, request):
        rig = self

        class Sandbox(LocalSandbox):
            async def write_bytes(self, path: str, data: bytes) -> None:
                await super().write_bytes(path, data)
                if rig.sandbox_hook is not None:
                    await rig.sandbox_hook()

        return Sandbox(request.workdir, request.baseline)

    async def start_executor(self, monkeypatch) -> FakeTestRunner:
        stop = self.stop

        class StopLifespan:
            def install_signal_handlers(self) -> None:
                return None

            async def wait(self) -> None:
                await stop.wait()

        monkeypatch.setattr(executor_app, "Lifespan", StopLifespan)
        runner = FakeTestRunner(output=b"3 passed in 0.10s\n")
        env = self.env
        settings = Settings(
            devlab=DevlabConfig(
                root=str(env.tmp_path),
                hub_url=env.hub_url,
                hub_token=env.executor_token,
                work_dir=str(env.tmp_path / "exec"),
                poll_interval_s=0.02,
                heartbeat_interval_s=0.05,
            )
        )
        self.executor = asyncio.create_task(
            executor_app.run_executor(settings, runner=runner, sandboxes=self.sandboxes)
        )
        return runner

    async def heartbeat_jobs(self) -> list[str] | None:
        cur = await self.env.store._db.conn.execute(
            "SELECT data FROM heartbeats WHERE name = 'pc' AND role = ?", (protocol.ROLE_EXECUTOR,)
        )
        row = await cur.fetchone()
        return json.loads(row["data"])[protocol.FIELD_JOBS] if row else None

    async def wait_status(self, job_id: str, status: str) -> None:
        async with asyncio.timeout(DONE_TIMEOUT_S):
            while await self.env.status(job_id) != status:
                await asyncio.sleep(0.02)

    async def requests_by_gateway(self, job_id: str) -> dict[str, int]:
        return {
            name: (await gateway.ledger.totals(MODEL, job_id))["requests"]
            for name, gateway in self.gateways.items()
        }


@pytest.fixture
async def rig(env):
    upstream = Upstream()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", upstream.chat)
    upstream_server = TestServer(app)
    await upstream_server.start_server()
    rig = Rig(env, upstream)
    for name in GATEWAYS:
        await rig.add_gateway(name, upstream_server)
    yield rig
    rig.upstream.gate.set()
    rig.stop.set()
    if rig.executor is not None:
        await asyncio.wait_for(rig.executor, 10)
    for server in rig.servers.values():
        with contextlib.suppress(Exception):
            await server.close()
    for gateway in rig.gateways.values():
        await gateway.ledger.close()
        for backend in gateway.backends.values():
            await backend.close()
    await upstream_server.close()


async def test_agent_job_runs_while_test_job_passes(env, rig, monkeypatch):
    job_id = await submit(env)
    await env.push(TEST_REF, {".devlab.toml": TEST_SPEC, "a.txt": b"x"})
    rig.upstream.gate.clear()
    runner = await rig.start_executor(monkeypatch)

    await wait_until(lambda: rig.upstream.requests == 1, timeout=DONE_TIMEOUT_S)
    assert await env.status(job_id) == protocol.STATUS_RUNNING
    tested = await env.mcp.call(
        protocol.TOOL_TEST, project=executor_tests.PROJECT, ref=TEST_REF, wait=DONE_TIMEOUT_S
    )
    assert tested["result"][protocol.PAYLOAD_EXIT_CODE] == 0
    assert len(runner.requests) == 1
    assert await env.status(job_id) == protocol.STATUS_RUNNING
    async with asyncio.timeout(DONE_TIMEOUT_S):
        while await rig.heartbeat_jobs() != [job_id]:
            await asyncio.sleep(0.02)

    rig.upstream.gate.set()
    await rig.wait_status(job_id, protocol.STATUS_DONE)

    assert sorted(await env.inbox_kinds()) == sorted([*STARTED_DONE, protocol.MSG_TEST_DONE])
    assert await hub_branch(env, wip_of(job_id)) is None
    agent_sha = await hub_branch(env, agent_of(job_id))
    assert await tracked_files(env, agent_sha) == sorted(BASE_FILES)
    done = await env.store.queue.last_message(job_id, protocol.MSG_DONE)
    assert done.payload[protocol.PAYLOAD_SUMMARY] == "сделано"
    async with asyncio.timeout(DONE_TIMEOUT_S):
        while await rig.heartbeat_jobs() != []:
            await asyncio.sleep(0.02)
    assert sum((await rig.requests_by_gateway(job_id)).values()) == 2


async def test_task_survives_gateway_loss_mid_job(env, rig, monkeypatch):
    job_id = await submit(env)

    async def kill_first_gateway() -> None:
        rig.sandbox_hook = None
        await rig.servers[GATEWAYS[0]].close()

    rig.sandbox_hook = kill_first_gateway
    await rig.start_executor(monkeypatch)

    await rig.wait_status(job_id, protocol.STATUS_DONE)

    assert await env.inbox_kinds() == STARTED_DONE
    assert await hub_branch(env, agent_of(job_id))
    assert await rig.requests_by_gateway(job_id) == {GATEWAYS[0]: 1, GATEWAYS[1]: 1}
