from __future__ import annotations

import asyncio
import logging

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.repo import (
    ABANDONED_PREFIX,
    MILESTONE_PREFIX,
    AgentRepo,
    agent_author,
)
from sa_home_bot.devlab.executor import mirror as mirror_module
from sa_home_bot.devlab.executor.mirror import GitError, ProjectMirror, remote_url
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.test_devlab_git import arun_git, auth

PROJECT = executor_tests.PROJECT
env = executor_tests.env
JOB = "job1"
BASE_BRANCH = f"{protocol.BASE_BRANCH_PREFIX}{JOB}"
WIP_BRANCH = f"{protocol.WIP_BRANCH_PREFIX}{JOB}"
AGENT_BRANCH = f"{protocol.AGENT_BRANCH_PREFIX}{JOB}"
BASE_FILES = {"a.txt": b"base\n"}
FAR = 3600.0


def make_repo(env, name="w", token=None, interval=0.0):
    mirror = ProjectMirror(
        env.tmp_path / f"{name}-mirror.git",
        remote_url(env.hub_url, PROJECT),
        token or env.executor_token,
    )
    return AgentRepo(
        mirror,
        env.tmp_path / name,
        env.tmp_path / f"{name}-git",
        JOB,
        agent_author("flint"),
        push_interval_s=interval,
    )


async def hub_refs(env) -> dict[str, str]:
    probe = env.tmp_path / "probe"
    probe.mkdir(exist_ok=True)
    out = await arun_git(probe, *auth(env.client_token), "ls-remote", env.git_url)
    assert out.returncode == 0, out.stderr
    pairs = (line.split("\t") for line in out.stdout.splitlines())
    return {ref: sha for sha, ref in pairs}


async def hub_branch(env, branch: str) -> str | None:
    return (await hub_refs(env)).get(f"{protocol.HEADS_REF_PREFIX}{branch}")


async def show(env, sha: str, fmt: str) -> str:
    probe = env.tmp_path / "probe"
    probe.mkdir(exist_ok=True)
    await arun_git(probe, "init", "-q")
    await arun_git(probe, *auth(env.client_token), "fetch", "-q", env.git_url, sha)
    return (await arun_git(probe, "log", "-1", f"--format={fmt}", sha)).stdout


async def tracked_files(env, sha: str) -> list[str]:
    await show(env, sha, "%H")
    out = await arun_git(env.tmp_path / "probe", "ls-tree", "-r", "--name-only", sha)
    return out.stdout.split()


async def test_full_cycle_commits_milestone_squash_and_wip_cleanup(env):
    base_sha = await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env, interval=FAR)
    await repo.open()
    assert (repo.workdir / "a.txt").read_bytes() == b"base\n"

    await repo.on_change("run_command: ls")
    assert await hub_branch(env, WIP_BRANCH) is None

    (repo.workdir / "a.txt").write_bytes(b"one\n")
    await repo.on_change("write_file: a.txt")
    first = await hub_branch(env, WIP_BRANCH)
    assert first and first != base_sha

    (repo.workdir / "b.txt").write_bytes(b"two\n")
    await repo.on_change("write_file: b.txt")
    (repo.workdir / "b.txt").write_bytes(b"three\n")
    await repo.on_change("edit_file: b.txt")
    assert await hub_branch(env, WIP_BRANCH) == first

    sha = await repo.milestone("шаг 1")
    assert await hub_branch(env, WIP_BRANCH) == sha
    assert (await show(env, sha, "%s")).strip() == f"{MILESTONE_PREFIX}шаг 1"

    (repo.workdir / "c.txt").write_bytes(b"tail\n")
    final = await repo.finish("Добавлены файлы")
    assert final

    refs = await hub_refs(env)
    assert f"{protocol.HEADS_REF_PREFIX}{WIP_BRANCH}" not in refs
    assert refs[f"{protocol.HEADS_REF_PREFIX}{AGENT_BRANCH}"] == final
    assert not (env.tmp_path / "w-git").exists()
    message = await show(env, final, "%B")
    assert message.startswith("Добавлены файлы")
    assert "- шаг 1" in message
    assert "devlab-agent (flint)" in await show(env, final, "%an")
    assert (await show(env, final, "%P")).strip() == base_sha
    assert await tracked_files(env, final) == ["a.txt", "b.txt", "c.txt"]
    assert (repo.workdir / "c.txt").exists()


async def test_empty_changes_make_no_commit(env):
    base_sha = await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env)
    await repo.open()
    await repo.on_change("run_command: pytest")
    await repo.flush()
    assert await hub_branch(env, WIP_BRANCH) is None
    assert await repo._head() == base_sha


async def test_deferred_push_goes_out_by_timer(env):
    await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env, interval=0.2)
    await repo.open()
    (repo.workdir / "a.txt").write_bytes(b"1\n")
    await repo.on_change("one")
    (repo.workdir / "a.txt").write_bytes(b"2\n")
    await repo.on_change("two")
    head = await repo._head()
    assert await hub_branch(env, WIP_BRANCH) != head
    async with asyncio.timeout(10):
        while await hub_branch(env, WIP_BRANCH) != head:
            await asyncio.sleep(0.05)
    await repo.aclose()


async def test_resume_continues_from_hub_wip_tip(env):
    await env.push(BASE_BRANCH, BASE_FILES)
    first = make_repo(env, "w1")
    await first.open()
    (first.workdir / "a.txt").write_bytes(b"progress\n")
    await first.on_change("write_file")
    tip = await first.milestone("шаг 1")

    second = make_repo(env, "w2")
    assert await second.open() == tip
    assert (second.workdir / "a.txt").read_bytes() == b"progress\n"
    (second.workdir / "d.txt").write_bytes(b"more\n")
    await second.on_change("write_file")
    assert await hub_branch(env, WIP_BRANCH) != tip
    sha = await second.finish("итог")
    assert sha
    assert "- шаг 1" in await show(env, sha, "%B")


async def test_resume_keeps_local_unpushed_work(env):
    await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env, interval=FAR)
    await repo.open()
    (repo.workdir / "a.txt").write_bytes(b"1\n")
    await repo.on_change("one")
    (repo.workdir / "a.txt").write_bytes(b"2\n")
    await repo.on_change("two")
    head = await repo._head()
    await repo.aclose()

    again = make_repo(env, interval=FAR)
    await again.open()
    assert await again._head() == head
    assert (again.workdir / "a.txt").read_bytes() == b"2\n"
    await again.flush()
    assert await hub_branch(env, WIP_BRANCH) == head


async def test_abandon_publishes_partial_result_and_drops_wip(env):
    await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env)
    await repo.open()
    (repo.workdir / "a.txt").write_bytes(b"half\n")
    await repo.on_change("write_file")
    sha = await repo.abandon("бюджет времени")
    assert await hub_branch(env, AGENT_BRANCH) == sha
    assert await hub_branch(env, WIP_BRANCH) is None
    assert (await show(env, sha, "%s")).startswith(f"{ABANDONED_PREFIX}бюджет времени")


async def test_abandon_without_changes_leaves_only_base(env):
    await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env)
    await repo.open()
    assert await repo.abandon("стоп") is None
    assert await hub_branch(env, AGENT_BRANCH) is None
    assert await hub_branch(env, WIP_BRANCH) is None


async def test_only_allowed_refs_are_touched(env):
    await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env)
    await repo.open()
    (repo.workdir / "a.txt").write_bytes(b"x\n")
    await repo.on_change("w")
    await repo.milestone("m")
    await repo.finish("f")
    assert set(await hub_refs(env)) == {
        f"{protocol.HEADS_REF_PREFIX}{BASE_BRANCH}",
        f"{protocol.HEADS_REF_PREFIX}{AGENT_BRANCH}",
    }


async def test_token_stays_out_of_argv_logs_and_errors(env, monkeypatch, caplog):
    seen: list[tuple[list[str], dict[str, str]]] = []
    real_run = mirror_module.run

    async def spy(argv, **kwargs):
        seen.append((list(argv), dict(kwargs.get("env") or {})))
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(mirror_module, "run", spy)
    caplog.set_level(logging.DEBUG)
    await env.push(BASE_BRANCH, BASE_FILES)
    repo = make_repo(env)
    await repo.open()
    (repo.workdir / "a.txt").write_bytes(b"x\n")
    await repo.on_change("w")
    await repo.milestone("m")
    await repo.finish("f")
    assert len(seen) > 10
    for argv, _ in seen:
        assert env.executor_token not in " ".join(argv)
    assert any(env.executor_token in environ.get("GIT_CONFIG_VALUE_0", "") for _, environ in seen)
    assert env.executor_token not in caplog.text

    bad = "dlh_" + "y" * 40
    with pytest.raises(GitError) as raised:
        await make_repo(env, "bad", token=bad).open()
    assert bad not in str(raised.value)
