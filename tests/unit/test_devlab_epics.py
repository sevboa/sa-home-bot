from __future__ import annotations

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import mcp_tools
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.service import HubService
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.utils.process import run
from tests.unit.support.fakes import FakeNodes

CLIENT = "claude"
OTHER = "other"
EXECUTOR = "mycraft"
MODEL = "qwen"
PROJECT = "demo"
BASE = "base/start"
S = protocol


def plan(count=3):
    return [
        {
            "title": f"шаг {i}",
            "task": f"# шаг {i}",
            "model": MODEL,
            "paths": [f"src/m{i}.py", f"tests/unit/test_m{i}.py"],
            "depends_on": [i - 1] if i else [],
        }
        for i in range(count)
    ]


@pytest.fixture
async def store(tmp_path):
    hub_store = HubStore(tmp_path)
    await hub_store.open()
    await hub_store.add_project(PROJECT)
    await git(
        hub_store,
        "update-ref",
        S.HEADS_REF_PREFIX + BASE,
        await commit(hub_store, None, "a.txt", "base"),
    )
    yield hub_store
    await hub_store.close()


def repo_of(store):
    return str(store.git_root / f"{PROJECT}{S.GIT_REPO_SUFFIX}")


async def git(store, *argv):
    result = await run(["git", "--git-dir", repo_of(store), *argv])
    assert result.returncode == 0, result.stderr_text
    return result.stdout_text.strip()


async def commit(store, parent, name, content):
    blob = await git_input(store, content)
    base_tree = await git(store, "rev-parse", f"{parent}^{{tree}}") if parent else None
    lines = []
    if base_tree:
        listing = await git(store, "ls-tree", base_tree)
        lines = [line for line in listing.splitlines() if not line.endswith("\t" + name)]
    lines.append(f"100644 blob {blob}\t{name}")
    tree = await mktree(store, "\n".join(lines) + "\n")
    args = ["-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", name]
    if parent:
        args += ["-p", parent]
    return await git(store, *args)


async def git_input(store, content):
    result = await run(
        ["git", "--git-dir", repo_of(store), "hash-object", "-w", "--stdin"],
        input=content.encode(),
    )
    return result.stdout_text.strip()


async def mktree(store, entries):
    result = await run(["git", "--git-dir", repo_of(store), "mktree"], input=entries.encode())
    assert result.returncode == 0, result.stderr_text
    return result.stdout_text.strip()


async def executor_finishes(store, job_id, name="f.txt", parent=None, content="x"):
    job = await store.queue.get_job(job_id)
    parent = parent or await git(store, "rev-parse", S.HEADS_REF_PREFIX + job.base_branch)
    sha = await commit(store, parent, name, content)
    await git(store, "update-ref", S.HEADS_REF_PREFIX + S.AGENT_BRANCH_PREFIX + job_id, sha)
    await store.queue.post_from_executor(job_id, S.MSG_DONE, {}, executor=EXECUTOR)
    return sha


async def claim(store):
    job = await store.queue.claim(EXECUTOR, [MODEL])
    assert job is not None
    return job


async def fail(store):
    job = await claim(store)
    await store.queue.post_from_executor(job.id, S.MSG_FAILED, {}, executor=EXECUTOR)
    return job


async def submit(store, tasks=None):
    return await store.epics.submit(CLIENT, PROJECT, BASE, plan() if tasks is None else tasks)


async def test_submit_creates_branch_and_only_first_job(store):
    view = await submit(store)
    branch = view["branch"]
    assert branch == S.EPIC_BRANCH_PREFIX + view["id"]
    assert await git(store, "rev-parse", S.HEADS_REF_PREFIX + branch) == await git(
        store, "rev-parse", S.HEADS_REF_PREFIX + BASE
    )
    jobs = await store.queue.list_jobs(CLIENT)
    assert len(jobs) == 1
    assert jobs[0].base_branch == branch
    assert jobs[0].params[S.PARAM_EPIC] == view["id"]
    assert jobs[0].params[S.PARAM_PATHS] == ["src/m0.py", "tests/unit/test_m0.py"]
    assert [t["job_id"] for t in view["tasks"]] == [jobs[0].id, None, None]
    assert view["current"] == 0


async def test_tasks_run_strictly_in_order_and_done_merges_into_epic_branch(store):
    view = await submit(store)
    epic_id = view["id"]
    heads = []
    for i in range(3):
        job = await claim(store)
        assert job.params[S.PARAM_EPIC_INDEX] == i
        assert await store.queue.claim(EXECUTOR, [MODEL]) is None
        heads.append(await executor_finishes(store, job.id, name=f"f{i}.txt"))
        now = await store.epics.view(CLIENT, epic_id)
        assert now["tasks"][i]["outcome"] == "done"
        assert now["tasks"][i]["commit"] == heads[-1]
        assert now["tasks"][i]["marked"]
        assert await git(store, "rev-parse", S.HEADS_REF_PREFIX + view["branch"]) == heads[-1]
    final = await store.epics.view(CLIENT, epic_id)
    assert final["status"] == S.EPIC_DONE
    assert final["current"] is None
    assert final["finished"]
    names = (
        await git(store, "ls-tree", "--name-only", S.HEADS_REF_PREFIX + view["branch"])
    ).split()
    assert names == ["a.txt", "f0.txt", "f1.txt", "f2.txt"]


async def test_second_task_starts_from_new_head(store):
    view = await submit(store)
    first = await claim(store)
    sha = await executor_finishes(store, first.id)
    second = await claim(store)
    assert second.base_branch == view["branch"]
    assert await git(store, "rev-parse", S.HEADS_REF_PREFIX + second.base_branch) == sha


async def test_diverged_agent_branch_gets_merge_commit(store):
    view = await submit(store, plan(1))
    job = await claim(store)
    base = await git(store, "rev-parse", S.HEADS_REF_PREFIX + BASE)
    side = await commit(store, base, "side.txt", "s")
    await git(store, "update-ref", S.HEADS_REF_PREFIX + view["branch"], side)
    await executor_finishes(store, job.id, name="mine.txt", parent=base)
    head = await git(store, "rev-parse", S.HEADS_REF_PREFIX + view["branch"])
    assert len((await git(store, "rev-list", "--parents", "-n1", head)).split()) == 3
    names = (await git(store, "ls-tree", "--name-only", head)).split()
    assert names == ["a.txt", "mine.txt", "side.txt"]


async def test_conflicting_merge_waits_for_owner(store):
    view = await submit(store, plan(2))
    job = await claim(store)
    base = await git(store, "rev-parse", S.HEADS_REF_PREFIX + BASE)
    await git(
        store,
        "update-ref",
        S.HEADS_REF_PREFIX + view["branch"],
        await commit(store, base, "a.txt", "left"),
    )
    await executor_finishes(store, job.id, name="a.txt", parent=base, content="right")
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_WAITING_OWNER
    assert "слить" in now["question"]
    assert now["tasks"][1]["job_id"] is None


async def test_first_failure_retries_second_stops_epic(store):
    view = await submit(store)
    first = await fail(store)
    again = await store.queue.get_job(first.id)
    assert again.status == S.STATUS_QUEUED
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_RUNNING
    assert now["tasks"][0]["failures"] == 1
    await fail(store)
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_WAITING_OWNER
    assert now["tasks"][0]["outcome"] == "failed"
    assert now["tasks"][0]["failures"] == 2
    assert now["tasks"][1]["job_id"] is None
    assert await store.queue.claim(EXECUTOR, [MODEL]) is None
    inbox = await store.queue.inbox(CLIENT)
    waiting = [m for m in inbox if m.kind == S.MSG_EPIC_WAITING]
    assert len(waiting) == 1
    assert view["id"] in waiting[0].payload[S.PARAM_EPIC]
    assert waiting[0].payload[S.PAYLOAD_TEXT] == now["question"]


async def test_owner_resume_after_wait_continues_epic(store):
    view = await submit(store, plan(2))
    await fail(store)
    job = await fail(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_RESUME, {})
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_RUNNING
    assert now["question"] == ""
    assert now["tasks"][0]["failures"] == 0
    claimed = await claim(store)
    assert claimed.id == job.id
    await executor_finishes(store, job.id)
    after = await store.epics.view(CLIENT, view["id"])
    assert after["tasks"][1]["job_id"] is not None


async def test_stopped_job_waits_for_owner(store):
    view = await submit(store)
    job = await claim(store)
    await store.queue.post_from_client(CLIENT, job.id, S.MSG_STOP, {})
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_WAITING_OWNER
    assert now["tasks"][0]["outcome"] == "stopped"
    assert now["tasks"][1]["job_id"] is None


async def test_resume_all_continues_from_last_mark_after_restart(store):
    view = await submit(store)
    first = await claim(store)
    store.queue.observers.clear()
    sha = await executor_finishes(store, first.id)
    mid = await store.epics.view(CLIENT, view["id"])
    assert mid["tasks"][0]["outcome"] == ""
    store.queue.observers.append(store.epics.sync)
    await store.epics.resume_all()
    now = await store.epics.view(CLIENT, view["id"])
    assert now["tasks"][0]["commit"] == sha
    assert now["tasks"][1]["job_id"] is not None
    await store.epics.resume_all()
    again = await store.epics.view(CLIENT, view["id"])
    assert again == now
    assert len(await store.queue.list_jobs(CLIENT)) == 2


async def test_stop_stops_current_job_and_blocks_progress(store):
    view = await submit(store)
    job = await claim(store)
    result = await store.epics.stop(view["id"])
    assert result == {"id": view["id"], "status": S.EPIC_STOPPED}
    assert (await store.queue.get_job(job.id)).status == S.STATUS_STOPPED
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_STOPPED
    assert now["tasks"][1]["job_id"] is None
    await store.epics.resume_all()
    assert (await store.epics.view(CLIENT, view["id"]))["status"] == S.EPIC_STOPPED
    assert await store.epics.active_summaries() == []


async def test_resume_returns_stopped_job_to_queue_and_epic_runs(store):
    view = await submit(store, plan(2))
    job = await claim(store)
    await store.epics.stop(view["id"])
    result = await store.epics.resume(view["id"])
    assert result["status"] == S.EPIC_RUNNING
    assert (await store.queue.get_job(job.id)).status == S.STATUS_QUEUED
    claimed = await claim(store)
    assert claimed.id == job.id
    await executor_finishes(store, job.id)
    after = await store.epics.view(CLIENT, view["id"])
    assert after["tasks"][1]["job_id"] is not None


async def test_resume_after_wait_resets_failures_and_requeues(store):
    view = await submit(store, plan(2))
    await fail(store)
    job = await fail(store)
    assert (await store.epics.view(CLIENT, view["id"]))["status"] == S.EPIC_WAITING_OWNER
    await store.epics.resume(view["id"])
    now = await store.epics.view(CLIENT, view["id"])
    assert now["status"] == S.EPIC_RUNNING
    assert now["tasks"][0]["failures"] == 0
    assert (await store.queue.get_job(job.id)).status == S.STATUS_QUEUED


async def test_finished_epic_cannot_be_resumed_or_stopped(store):
    view = await submit(store, plan(1))
    job = await claim(store)
    await executor_finishes(store, job.id)
    for action in (store.epics.resume, store.epics.stop):
        with pytest.raises(HubError, match="завершён"):
            await action(view["id"])


async def test_active_summaries_and_task_totals(store):
    view = await submit(store, plan(3))
    job = await claim(store)
    await executor_finishes(store, job.id)
    [summary] = await store.epics.active_summaries()
    assert summary["id"] == view["id"]
    assert summary["title"] == "шаг 0"
    assert (summary["done"], summary["total"]) == (1, 3)
    assert summary["current_title"] == "шаг 1"
    assert summary["status"] == S.EPIC_RUNNING
    assert await store.epics.task_totals([view["id"], "zzzzzz"]) == {view["id"]: 3}
    assert await store.epics.task_totals([]) == {}


async def test_owner_jobs_carry_epics_and_epic_total(store):
    view = await submit(store, plan(2))
    await claim(store)
    service = HubService(store, FakeNodes())
    reply = await service.run_command(S.ACTION_OWNER_JOBS, {})
    assert [e["id"] for e in reply[S.FIELD_EPICS]] == [view["id"]]
    assert reply[S.FIELD_JOBS][0][S.FIELD_EPIC_TOTAL] == 2


async def test_owner_epic_post_resume_and_stop(store):
    view = await submit(store)
    await claim(store)
    service = HubService(store, FakeNodes())
    stopped = await service.run_command(
        S.ACTION_OWNER_EPIC_POST, {"id": view["id"], "kind": S.MSG_STOP}
    )
    assert stopped["status"] == S.EPIC_STOPPED
    resumed = await service.run_command(
        S.ACTION_OWNER_EPIC_POST, {"id": view["id"], "kind": S.MSG_RESUME}
    )
    assert resumed["status"] == S.EPIC_RUNNING
    with pytest.raises(ProtoError):
        await service.run_command(
            S.ACTION_OWNER_EPIC_POST, {"id": view["id"], "kind": S.MSG_ANSWER}
        )
    with pytest.raises(ProtoError):
        await service.run_command(S.ACTION_OWNER_EPIC_POST, {"id": "zzzzzz", "kind": S.MSG_STOP})


async def test_mcp_epic_tools_respect_owner_and_status_shows_live(store):
    view = await submit(store)
    job = await claim(store)
    await store.queue.note_live(
        {job.id: {S.FIELD_CONTEXT_TOKENS: 46000, S.FIELD_CONTEXT_LIMIT: 118000, S.FIELD_FOLDS: 2}}
    )
    status = await mcp_tools.status(mcp_tools.Context(store, CLIENT), job.id)
    assert status[S.FIELD_CONTEXT_TOKENS] == 46000
    assert status[S.FIELD_FOLDS] == 2
    with pytest.raises(HubError, match="нет"):
        await mcp_tools.epic_stop(mcp_tools.Context(store, OTHER), view["id"])
    result = await mcp_tools.epic_stop(mcp_tools.Context(store, CLIENT), view["id"])
    assert result["status"] == S.EPIC_STOPPED
    result = await mcp_tools.epic_resume(mcp_tools.Context(store, CLIENT), view["id"])
    assert result["status"] == S.EPIC_RUNNING


async def test_epic_is_visible_only_to_its_client(store):
    view = await submit(store)
    with pytest.raises(HubError, match="нет"):
        await store.epics.view(OTHER, view["id"])
    with pytest.raises(HubError, match="нет"):
        await store.epics.view(CLIENT, "zzzzzz")


async def test_bad_plan_is_refused_with_reasons_and_creates_nothing(store):
    bad = plan(2)
    bad[0]["paths"] = []
    bad[1]["depends_on"] = [1]
    with pytest.raises(HubError) as info:
        await submit(store, bad)
    assert "paths пуст" in str(info.value)
    assert "цикл" in str(info.value)
    assert await store.queue.list_jobs(CLIENT) == []


async def test_existing_test_basename_in_base_is_refused(store):
    base = await git(store, "rev-parse", S.HEADS_REF_PREFIX + BASE)
    tests_tree = await mktree(store, f"100644 blob {await git_input(store, 'x')}\ttest_m0.py\n")
    root = await mktree(
        store,
        f"100644 blob {await git_input(store, 'x')}\ta.txt\n040000 tree {tests_tree}\ttests\n",
    )
    sha = await git(
        store,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "commit-tree",
        root,
        "-p",
        base,
        "-m",
        "t",
    )
    await git(store, "update-ref", S.HEADS_REF_PREFIX + BASE, sha)
    with pytest.raises(HubError, match="уже есть в базе"):
        await submit(store, plan(1))


@pytest.mark.parametrize(
    ("tasks", "fragment"),
    [
        ([], "без задач"),
        (["x"], "ожидался объект"),
        ([{"title": "t", "task": "x", "model": "m", "paths": ["a"], "bogus": 1}], "неизвестные"),
        ([{"title": "t", "task": "", "model": "m", "paths": ["a"]}], "нужны title"),
        ([{"title": "t", "task": "x", "model": "m", "paths": "a"}], "список строк"),
        (
            [{"title": "t", "task": "x", "model": "m", "paths": ["a"], "depends_on": ["0"]}],
            "индексов",
        ),
    ],
)
async def test_malformed_tasks_are_refused(store, tasks, fragment):
    with pytest.raises(HubError, match=fragment):
        await submit(store, tasks)


async def test_unknown_base_branch_is_refused(store):
    with pytest.raises(HubError, match="нет ветки"):
        await store.epics.submit(CLIENT, PROJECT, "base/nope", plan(1))
    with pytest.raises(HubError, match="базовая ветка"):
        await store.epics.submit(CLIENT, PROJECT, "master", plan(1))


async def test_jobs_get_later_test_files_to_skip_and_tester_files_locked(store):
    tasks = plan()
    tasks[0]["profile"] = S.PROFILE_TESTER
    tasks[2]["profile"] = S.PROFILE_TESTER
    view = await submit(store, tasks)
    seen = []
    for i in range(3):
        job = await claim(store)
        seen.append(
            (job.params[S.PARAM_EPIC_SKIP_TESTS], job.params[S.PARAM_EPIC_LOCKED])
        )
        await executor_finishes(store, job.id, name=f"f{i}.txt")
    assert seen == [
        (["tests/unit/test_m1.py", "tests/unit/test_m2.py"], []),
        (["tests/unit/test_m2.py"], ["src/m0.py", "tests/unit/test_m0.py"]),
        ([], []),
    ]
    assert view["tasks"][0]["profile"] == S.PROFILE_TESTER


async def test_job_outside_epic_has_no_skip_or_locked(store):
    job = await store.queue.submit(CLIENT, PROJECT, MODEL, "задача", base_branch=BASE)
    assert S.PARAM_EPIC_SKIP_TESTS not in job.params
    assert S.PARAM_EPIC_LOCKED not in job.params
