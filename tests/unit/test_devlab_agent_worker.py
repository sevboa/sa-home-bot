from __future__ import annotations

import asyncio
from typing import Any

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.model import ModelError
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.executor.agent_worker import (
    AGENT_DIR,
    AGENT_STATE_FILE,
    AgentSettings,
    AgentState,
    AgentWorker,
)
from sa_home_bot.devlab.executor.hub import ExecutorHub, HubRequestError
from sa_home_bot.utils.files import load_model
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import ScriptedChatModel, tool_reply
from tests.unit.test_devlab_agent_repo import hub_branch, tracked_files

env = executor_tests.env
PROJECT = executor_tests.PROJECT
MODEL = "flint"
BASE = "base/feature"
SPEC = (
    b'[test]\ncmd = "sh test.sh"\ntimeout_s = 60\n'
    b'[agent]\nrules = ["RULES.md"]\nforbidden_paths = ["test.sh"]\n'
)
BASE_FILES = {
    ".devlab.toml": SPEC,
    "RULES.md": "правило-один\n".encode(),
    "test.sh": b"grep -q done out.txt\n",
    "out.txt": b"todo\n",
}
WRITE_DONE = tool_reply("write_file", "w1", path="out.txt", content="done\n")
FINISH = tool_reply("finish", "f1", summary="сделано")
STARTED_DONE = [protocol.MSG_STARTED, protocol.MSG_DONE]


class BlockingModel(ScriptedChatModel):
    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self.waiting = asyncio.Event()

    async def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        if not self.script:
            self.waiting.set()
            await asyncio.Event().wait()
        return await super().complete(messages, tools)


class FlakyHub(ExecutorHub):
    down = False

    async def post(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        if self.down:
            raise HubRequestError("/api/post: ClientConnectorError")
        await super().post(job_id, kind, payload)


def make_worker(
    env, model, hub_class=ExecutorHub, models=(MODEL,), sandboxes=None, limits=None
) -> AgentWorker:
    hub = hub_class(env.hub_url, env.executor_token)
    env.hubs.append(hub)
    return AgentWorker(
        hub,
        lambda model_name, job_id: model,
        sandboxes or (lambda request: LocalSandbox(request.workdir, request.baseline)),
        AgentSettings(
            hub_url=env.hub_url,
            token=env.executor_token,
            work_dir=env.tmp_path / "exec",
            poll_interval_s=0.01,
            heartbeat_interval_s=0.01,
            models=models,
            **({"limits": limits} if limits else {}),
            push_interval_s=0.0,
            retry_interval_s=0.01,
        ),
    )


async def submit(env) -> str:
    await env.push(BASE, BASE_FILES)
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=PROJECT,
        model=MODEL,
        task="# сделай out.txt",
        base_branch=BASE,
    )
    return data["job"]["id"]


async def start_blocked(worker: AgentWorker, model: BlockingModel) -> asyncio.Task:
    running = asyncio.create_task(worker.claim_once())
    async with asyncio.timeout(20):
        await model.waiting.wait()
    return running


def wip_of(job_id: str) -> str:
    return f"{protocol.WIP_BRANCH_PREFIX}{job_id}"


def agent_of(job_id: str) -> str:
    return f"{protocol.AGENT_BRANCH_PREFIX}{job_id}"


async def test_full_path_to_agent_branch(env):
    job_id = await submit(env)
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker = make_worker(env, model)

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == STARTED_DONE
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))
    agent_sha = await hub_branch(env, agent_of(job_id))
    assert await tracked_files(env, agent_sha) == sorted(BASE_FILES)
    assert "правило-один" in model.seen[0][0]["content"]
    assert model.seen[0][1]["content"] == "# сделай out.txt"
    assert (await env.store.queue.get_job(job_id)).tokens == 220
    done = await env.store.queue.last_message(job_id, protocol.MSG_DONE)
    assert done.payload[protocol.PAYLOAD_SUMMARY] == "сделано"
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()
    assert load_model(AgentState, env.tmp_path / "exec" / AGENT_STATE_FILE).job is None
    assert not worker.pending_messages


async def test_claim_returns_false_without_jobs(env):
    assert not await make_worker(env, ScriptedChatModel()).claim_once()


async def test_stop_closes_job_and_keeps_partial_branch(env):
    job_id = await submit(env)
    model = BlockingModel(WRITE_DONE)
    worker = make_worker(env, model)
    running = await start_blocked(worker, model)

    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await worker.poll_once()
    assert await running

    assert await env.status(job_id) == protocol.STATUS_STOPPED
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))
    assert worker.cursor > 0
    assert not worker.pending_messages


async def test_restart_continues_from_wip(env):
    job_id = await submit(env)
    first_model = BlockingModel(WRITE_DONE)
    running = await start_blocked(make_worker(env, first_model), first_model)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await hub_branch(env, wip_of(job_id))
    assert await env.status(job_id) == protocol.STATUS_RUNNING

    model = ScriptedChatModel(FINISH)
    assert await make_worker(env, model).claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == STARTED_DONE
    assert "out.txt" in model.seen[0][1]["content"]
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))


async def test_outbox_survives_unreachable_hub_and_restart(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, FINISH), FlakyHub)
    worker._hub.down = True

    assert await worker.claim_once()

    assert [item.kind for item in worker.pending_messages] == STARTED_DONE
    assert await env.status(job_id) == protocol.STATUS_RUNNING

    restarted = make_worker(env, ScriptedChatModel())
    assert [item.kind for item in restarted.pending_messages] == STARTED_DONE
    await restarted.drain()
    assert not restarted.pending_messages
    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == STARTED_DONE


async def test_model_failure_marks_job_failed_and_keeps_partial(env):
    job_id = await submit(env)
    worker = make_worker(env, ScriptedChatModel(WRITE_DONE, ModelError("шлюз лёг")))

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_FAILED
    failed = await env.store.queue.last_message(job_id, protocol.MSG_FAILED)
    assert "шлюз лёг" in failed.payload[protocol.PAYLOAD_TEXT]
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))
