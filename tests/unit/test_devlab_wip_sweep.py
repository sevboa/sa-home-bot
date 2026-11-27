from __future__ import annotations

from datetime import timedelta

from sa_home_bot.devlab import protocol
from sa_home_bot.utils.clock import utc_now
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.test_devlab_agent_repo import hub_branch

env = executor_tests.env


async def make_job(env, name: str, ending: str | None) -> str:
    base = f"base/{name}"
    await env.push(base, {"a.txt": b"base"})
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=executor_tests.PROJECT,
        model="flint",
        task="# задача",
        base_branch=base,
    )
    job_id = data["job"]["id"]
    await env.store.queue.claim("exec", ["flint"])
    if ending is not None:
        await env.store.queue.post_from_executor(job_id, ending, {protocol.PAYLOAD_TEXT: "x"})
    return job_id


def wip(job_id: str) -> str:
    return f"{protocol.WIP_BRANCH_PREFIX}{job_id}"


async def test_purge_removes_old_ended_and_orphaned_wip_only(env):
    stopped = await make_job(env, "stopped", protocol.MSG_STOPPED)
    failed = await make_job(env, "failed", protocol.MSG_FAILED)
    running = await make_job(env, "running", None)
    branches = [wip(stopped), wip(failed), wip(running), wip("orphan1"), "agent/agent1"]
    for branch in branches:
        await env.push(branch, {"a.txt": b"x"}, env.executor_token)

    await env.store.queue.purge(5, now=utc_now() + timedelta(days=6))

    assert await hub_branch(env, wip(stopped)) is None
    assert await hub_branch(env, wip(failed)) is None
    assert await hub_branch(env, wip("orphan1")) is None
    assert await hub_branch(env, wip(running)) is not None
    assert await hub_branch(env, "agent/agent1") is not None


async def test_purge_keeps_recent_ended_wip(env):
    stopped = await make_job(env, "stopped", protocol.MSG_STOPPED)
    failed = await make_job(env, "failed", protocol.MSG_FAILED)
    for job_id in (stopped, failed):
        await env.push(wip(job_id), {"a.txt": b"x"}, env.executor_token)

    await env.store.queue.purge(5)

    assert await hub_branch(env, wip(stopped)) is not None
    assert await hub_branch(env, wip(failed)) is not None
