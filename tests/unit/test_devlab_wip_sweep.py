from __future__ import annotations

from datetime import timedelta

from sa_home_bot.devlab import protocol
from sa_home_bot.utils.clock import utc_now
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.test_devlab_agent_repo import hub_branch
from tests.unit.test_devlab_git import arun_git, auth

env = executor_tests.env


async def _push_wip_branch(env, branch: str):
    work = env.tmp_path / f"snap-{branch.replace('/', '-')}"
    work.mkdir()
    run_git = executor_tests.run_git
    run_git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_bytes(b"x")
    run_git(work, "add", "-A")
    run_git(work, "commit", "-q", "-m", "snap")
    pushed = await arun_git(
        work, *auth(env.executor_token), "push", env.git_url, f"main:refs/heads/{branch}"
    )
    assert pushed.returncode == 0, pushed.stderr


async def _make_job_stopped(env, store):
    base = "base/stopped"
    await env.push(base, {"a.txt": b"base"})
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=executor_tests.PROJECT,
        model="flint",
        task="# задача",
        base_branch=base,
    )
    job_id = data["job"]["id"]
    await store.queue.claim("exec", ["flint"])
    await store.queue.post_from_executor(job_id, protocol.MSG_STOPPED, {})
    return job_id


async def _make_job_failed(env, store):
    base = "base/failed"
    await env.push(base, {"a.txt": b"base"})
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=executor_tests.PROJECT,
        model="flint",
        task="# задача",
        base_branch=base,
    )
    job_id = data["job"]["id"]
    await store.queue.claim("exec", ["flint"])
    await store.queue.post_from_executor(
        job_id, protocol.MSG_FAILED, {protocol.PAYLOAD_TEXT: "fail"}
    )
    return job_id


async def _make_job_running(env, store):
    base = "base/running"
    await env.push(base, {"a.txt": b"base"})
    data = await env.mcp.call(
        protocol.TOOL_SUBMIT,
        project=executor_tests.PROJECT,
        model="flint",
        task="# задача",
        base_branch=base,
    )
    job_id = data["job"]["id"]
    await store.queue.claim("exec", ["flint"])
    return job_id


async def test_purge_removes_wip_branches_for_old_stopped_failed_and_orphaned(env):
    store = env.store
    stopped_job = await _make_job_stopped(env, store)
    failed_job = await _make_job_failed(env, store)
    running_job = await _make_job_running(env, store)

    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{stopped_job}")
    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{failed_job}")
    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{running_job}")
    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}orphan1")
    await _push_wip_branch(env, f"{protocol.AGENT_BRANCH_PREFIX}agent1")

    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{stopped_job}") is not None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{failed_job}") is not None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{running_job}") is not None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}orphan1") is not None
    assert await hub_branch(env, f"{protocol.AGENT_BRANCH_PREFIX}agent1") is not None

    future = utc_now() + timedelta(days=6)
    await store.queue.purge(5, now=future)

    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{stopped_job}") is None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{failed_job}") is None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{running_job}") is not None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}orphan1") is None
    assert await hub_branch(env, f"{protocol.AGENT_BRANCH_PREFIX}agent1") is not None


async def test_purge_keeps_recent_stopped_failed_wip_branches(env):
    store = env.store
    recent_stopped = await _make_job_stopped(env, store)
    recent_failed = await _make_job_failed(env, store)

    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{recent_stopped}")
    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{recent_failed}")

    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{recent_stopped}") is not None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{recent_failed}") is not None

    await store.queue.purge(5)

    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{recent_stopped}") is not None
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{recent_failed}") is not None


async def test_purge_removes_wip_branch_for_job_not_in_db(env):
    store = env.store
    orphan_job_id = "orphan999"

    await _push_wip_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{orphan_job_id}")
    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{orphan_job_id}") is not None

    await store.queue.purge(30)

    assert await hub_branch(env, f"{protocol.WIP_BRANCH_PREFIX}{orphan_job_id}") is None
