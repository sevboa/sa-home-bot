from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, time, timedelta

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.queue import SCHEMA as S_QUEUE_SCHEMA
from sa_home_bot.devlab.hub.queue import TransitionRefused, fix_task_text, in_windows
from sa_home_bot.devlab.hub.store import HUB_DB_NAME, HubStore
from sa_home_bot.utils.clock import utc_now

CLIENT = "claude-laptop"
OTHER_CLIENT = "claude-pc"
EXECUTOR = "mycraft"
MODEL = "qwen-local"
OTHER_MODEL = "sonnet-remote"
PROJECT = "demo"
TASK = "# цель\nсделать"

S = protocol


@pytest.fixture
async def queue(tmp_path):
    store = HubStore(tmp_path)
    await store.open()
    await store.queue.add_project(PROJECT)
    yield store.queue
    await store.close()


async def submit(
    queue, model=MODEL, priority=0, client=CLIENT, purpose=S.PURPOSE_DEV, depends_on=(), fallback=""
):
    return await queue.submit(
        client, PROJECT, model, TASK, priority, purpose=purpose, depends_on=depends_on,
        fallback=fallback,
    )


async def running_job(queue):
    job = await submit(queue)
    await queue.claim(EXECUTOR, [MODEL])
    return job


@pytest.fixture
async def bench_queue(tmp_path):
    store = HubStore(tmp_path, bench_windows=["01:00-07:00"])
    await store.open()
    await store.queue.add_project(PROJECT)
    yield store.queue
    await store.close()


WINDOWS_CASES = [
    ([], time(12, 0), True),
    (["01:00-07:00"], time(3, 0), True),
    (["01:00-07:00"], time(7, 0), False),
    (["23:00-07:00"], time(23, 30), True),
    (["23:00-07:00"], time(6, 59), True),
    (["23:00-07:00"], time(12, 0), False),
]


@pytest.mark.parametrize(("windows", "moment", "expected"), WINDOWS_CASES)
def test_in_windows(windows, moment, expected):
    assert in_windows(windows, moment) is expected


def test_in_windows_bad():
    with pytest.raises(ValueError, match="25:00-x"):
        in_windows(["25:00-x"], time(0, 0))


async def test_bench_claim_outside_window(bench_queue):
    bench = await submit(bench_queue, purpose=S.PURPOSE_BENCH)
    assert await bench_queue.claim(EXECUTOR, [MODEL], now=datetime(2026, 11, 18, 12, 0)) is None
    assert await bench_queue.pending_work(now=datetime(2026, 11, 18, 12, 0)) is False
    assert await bench_queue.pending_work(now=datetime(2026, 11, 18, 3, 0)) is True
    found = await bench_queue.claim(EXECUTOR, [MODEL], now=datetime(2026, 11, 18, 3, 0))
    assert found.id == bench.id


async def test_dev_claim_outside_bench_window(bench_queue):
    job = await submit(bench_queue)
    claimed = await bench_queue.claim(EXECUTOR, [MODEL], now=datetime(2026, 11, 18, 12, 0))
    assert claimed.id == job.id


async def waiting_job(queue):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
    return job


async def finished_job(queue, kind):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, kind)
    return job


SETUPS = {
    S.STATUS_QUEUED: submit,
    S.STATUS_RUNNING: running_job,
    S.STATUS_WAITING_OWNER: waiting_job,
    S.STATUS_DONE: lambda q: finished_job(q, S.MSG_DONE),
    S.STATUS_FAILED: lambda q: finished_job(q, S.MSG_FAILED),
    S.STATUS_STOPPED: lambda q: finished_job(q, S.MSG_STOPPED),
}

CLIENT_ACTIONS = {
    S.MSG_ANSWER: S.STATUS_RUNNING,
    S.MSG_RESUME: S.STATUS_QUEUED,
    S.MSG_STOP: S.STATUS_STOPPED,
}

EXECUTOR_ACTIONS = {
    S.MSG_QUESTION: S.STATUS_WAITING_OWNER,
    S.MSG_DONE: S.STATUS_DONE,
    S.MSG_FAILED: S.STATUS_FAILED,
    S.MSG_STOPPED: S.STATUS_STOPPED,
}


def test_transition_table_covers_all_statuses():
    assert set(S.TRANSITIONS) == set(S.STATUSES)
    assert all(
        S.STATUS_STOPPED in S.TRANSITIONS[s] for s in S.STATUSES if s not in S.TERMINAL_STATUSES
    )


@pytest.mark.parametrize("status", S.STATUSES)
@pytest.mark.parametrize("kind", CLIENT_ACTIONS)
async def test_client_actions_follow_transition_table(queue, status, kind):
    job = await SETUPS[status](queue)
    allowed = CLIENT_ACTIONS[kind] in S.TRANSITIONS[status]
    if allowed:
        await queue.post_from_client(CLIENT, job.id, kind)
        assert (await queue.get_job(job.id)).status == CLIENT_ACTIONS[kind]
    else:
        with pytest.raises(HubError):
            await queue.post_from_client(CLIENT, job.id, kind)
        assert (await queue.get_job(job.id)).status == status


@pytest.mark.parametrize("status", S.STATUSES)
@pytest.mark.parametrize("kind", EXECUTOR_ACTIONS)
async def test_executor_actions_follow_transition_table(queue, status, kind):
    job = await SETUPS[status](queue)
    target = EXECUTOR_ACTIONS[kind]
    confirms_stop = kind == S.MSG_STOPPED and status == S.STATUS_STOPPED
    repeats_question = kind == S.MSG_QUESTION and status == S.STATUS_WAITING_OWNER
    if target in S.TRANSITIONS[status] or confirms_stop or repeats_question:
        await queue.post_from_executor(job.id, kind)
        assert (await queue.get_job(job.id)).status == target
    else:
        with pytest.raises(HubError):
            await queue.post_from_executor(job.id, kind)


@pytest.mark.parametrize("kind", [S.MSG_STARTED, S.MSG_STEP_DONE])
@pytest.mark.parametrize("status", S.STATUSES)
async def test_progress_messages_only_while_running(queue, kind, status):
    job = await SETUPS[status](queue)
    if status == S.STATUS_RUNNING:
        await queue.post_from_executor(job.id, kind)
        assert (await queue.get_job(job.id)).status == S.STATUS_RUNNING
    else:
        with pytest.raises(HubError):
            await queue.post_from_executor(job.id, kind)


async def test_submit_defaults_and_validation(queue):
    job = await submit(queue)
    assert job.status == S.STATUS_QUEUED
    assert job.base_branch == f"base/{job.id}"
    assert len(job.id) == 6
    assert (await submit(queue)).id != job.id
    with pytest.raises(HubError):
        await queue.submit(CLIENT, "нет-такого", MODEL, TASK)
    with pytest.raises(HubError):
        await queue.submit(CLIENT, PROJECT, " ", TASK)
    with pytest.raises(HubError):
        await queue.add_project(PROJECT)
    with pytest.raises(HubError):
        await queue.add_project("Плохое имя")
    assert await queue.projects() == [PROJECT]


@pytest.mark.parametrize(
    "profile, expected",
    [
        (None, "executor"),
        ("tester", "tester"),
        ("executor-xs", "executor-xs"),
        ("docs", "docs"),
        ("devops", "devops"),
    ],
)
async def test_submit_profile(queue, profile, expected):
    kwargs = {"profile": profile} if profile is not None else {}
    job = await queue.submit(CLIENT, PROJECT, MODEL, TASK, **kwargs)
    assert job.params == {S.PARAM_PROFILE: expected, S.PARAM_PURPOSE: S.PURPOSE_DEV}


@pytest.mark.parametrize("bad_profile", ["Плохой", "", "a b", "A", "a.b"])
async def test_submit_rejects_bad_profile(queue, bad_profile):
    with pytest.raises(HubError, match="профиль"):
        await queue.submit(CLIENT, PROJECT, MODEL, TASK, profile=bad_profile)


async def test_resume_preserves_profile(queue):
    job = await finished_job(queue, S.MSG_FAILED)
    assert job.params[S.PARAM_PROFILE] == "executor"
    await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME, {S.PAYLOAD_TEXT: "замечание"})
    resumed = await queue.get_job(job.id)
    assert resumed.params[S.PARAM_PROFILE] == "executor"


async def test_claim_order_priority_then_age(queue):
    low = await submit(queue)
    high = await submit(queue, priority=5)
    low2 = await submit(queue)
    order = [(await queue.claim(EXECUTOR, [MODEL])).id for _ in range(3)]
    assert order == [high.id, low.id, low2.id]
    assert await queue.claim(EXECUTOR, [MODEL]) is None


@pytest.mark.parametrize(
    "first_purpose, first_priority, second_purpose, second_priority, expected_order",
    [
        (S.PURPOSE_BENCH, 9, S.PURPOSE_DEV, 0, ["dev", "bench"]),
        (S.PURPOSE_DEV, 0, S.PURPOSE_BENCH, 9, ["dev", "bench"]),
        (S.PURPOSE_DEV, 9, S.PURPOSE_FIX, 0, ["fix", "dev"]),
        (S.PURPOSE_FIX, 0, S.PURPOSE_DEV, 9, ["fix", "dev"]),
        (S.PURPOSE_BENCH, 9, S.PURPOSE_FIX, 0, ["fix", "bench"]),
        (S.PURPOSE_FIX, 0, S.PURPOSE_BENCH, 9, ["fix", "bench"]),
    ],
)
async def test_claim_order_dev_before_bench(
    queue, first_purpose, first_priority, second_purpose, second_priority, expected_order
):
    await submit(queue, priority=first_priority, purpose=first_purpose)
    await submit(queue, priority=second_priority, purpose=second_purpose)
    claimed = []
    for _ in range(2):
        job = await queue.claim(EXECUTOR, [MODEL])
        assert job is not None
        claimed.append(job.params[S.PARAM_PURPOSE])
    assert claimed == expected_order
    assert await queue.claim(EXECUTOR, [MODEL]) is None


async def test_fix_with_unclosed_dependency_yields_to_dev(queue):
    x = await submit(queue, priority=0, purpose=S.PURPOSE_DEV)
    a = await submit(queue, priority=0, purpose=S.PURPOSE_FIX, depends_on=[x.id])
    b = await submit(queue, priority=5, purpose=S.PURPOSE_DEV)
    first = await queue.claim(EXECUTOR, [MODEL])
    assert first.id == b.id
    assert first.params[S.PARAM_PURPOSE] == S.PURPOSE_DEV
    second = await queue.claim(EXECUTOR, [MODEL])
    assert second.id == x.id
    assert second.params[S.PARAM_PURPOSE] == S.PURPOSE_DEV
    assert await queue.claim(EXECUTOR, [MODEL]) is None
    await queue.post_from_executor(b.id, S.MSG_DONE)
    await queue.post_from_executor(x.id, S.MSG_DONE)
    third = await queue.claim(EXECUTOR, [MODEL])
    assert third.id == a.id
    assert third.params[S.PARAM_PURPOSE] == S.PURPOSE_FIX


async def test_claim_only_bench_in_queue(queue):
    bench = await submit(queue, priority=5, purpose=S.PURPOSE_BENCH)
    claimed = await queue.claim(EXECUTOR, [MODEL])
    assert claimed is not None
    assert claimed.id == bench.id
    assert claimed.params[S.PARAM_PURPOSE] == S.PURPOSE_BENCH
    assert await queue.claim(EXECUTOR, [MODEL]) is None


async def test_depends_on_blocks_until_done(queue):
    a = await submit(queue, priority=0)
    b = await submit(queue, priority=5, depends_on=[a.id])
    first = await queue.claim(EXECUTOR, [MODEL])
    assert first.id == a.id
    assert await queue.claim(EXECUTOR, [MODEL]) is None
    await queue.post_from_executor(a.id, S.MSG_DONE)
    second = await queue.claim(EXECUTOR, [MODEL])
    assert second.id == b.id


@pytest.mark.parametrize(
    "fail_via, expected_status",
    [
        ("executor_failed", S.STATUS_FAILED),
        ("executor_stopped", S.STATUS_STOPPED),
        ("client_stop", S.STATUS_STOPPED),
    ],
)
async def test_dependency_failed_notifies_dependents(queue, fail_via, expected_status):
    a = await submit(queue)
    b = await submit(queue, depends_on=[a.id])
    c = await submit(queue)

    if fail_via == "executor_failed":
        await queue.claim(EXECUTOR, [MODEL])
        await queue.post_from_executor(a.id, S.MSG_FAILED, {S.PAYLOAD_TEXT: "oops"})
    elif fail_via == "executor_stopped":
        await queue.claim(EXECUTOR, [MODEL])
        await queue.post_from_executor(a.id, S.MSG_STOPPED)
    else:
        await queue.post_from_client(CLIENT, a.id, S.MSG_STOP)

    inbox = await queue.inbox(CLIENT)
    dep_msgs = [m for m in inbox if m.kind == S.MSG_DEPENDENCY_FAILED]
    assert len(dep_msgs) == 1
    msg = dep_msgs[0]
    assert msg.job_id == b.id
    assert msg.payload == {S.PAYLOAD_DEPENDENCY: a.id, S.PAYLOAD_DEPENDENCY_STATUS: expected_status}

    assert (await queue.get_job(b.id)).status == S.STATUS_QUEUED
    assert (await queue.get_job(c.id)).status == S.STATUS_QUEUED

    c_inbox = [m for m in inbox if m.job_id == c.id and m.kind == S.MSG_DEPENDENCY_FAILED]
    assert len(c_inbox) == 0


async def test_depends_on_missing_raises(queue):
    with pytest.raises(HubError, match="зависимость"):
        await queue.submit(CLIENT, PROJECT, MODEL, TASK, depends_on=["nope"])


async def test_depends_on_other_client_raises(queue):
    other = await submit(queue, client=OTHER_CLIENT)
    with pytest.raises(HubError, match="зависимость"):
        await queue.submit(CLIENT, PROJECT, MODEL, TASK, depends_on=[other.id])


async def test_submit_without_depends_on_has_no_key(queue):
    job = await submit(queue)
    assert S.PARAM_DEPENDS_ON not in job.params


async def test_submit_fallback_same_as_model_raises(queue):
    with pytest.raises(HubError, match="совпадает"):
        await queue.submit(CLIENT, PROJECT, MODEL, TASK, fallback=MODEL)


async def test_fallback_claimed_when_primary_busy(queue):
    plain = await submit(queue)
    job = await submit(queue, fallback="remote")
    claimed = await queue.claim(EXECUTOR, [MODEL])
    assert claimed.id == plain.id
    claimed = await queue.claim(EXECUTOR, ["remote"])
    assert claimed.id == job.id
    assert claimed.model == "remote"
    assert claimed.params[S.PARAM_ORIGINAL_MODEL] == MODEL
    assert claimed.params[S.PARAM_FALLBACK] == "remote"


async def test_fallback_not_taken_when_primary_free_and_live(queue):
    await submit(queue, fallback="remote")
    assert await queue.claim(EXECUTOR, ["remote"], live_models=[MODEL, "remote"]) is None


async def test_fallback_taken_when_primary_not_live(queue):
    job = await submit(queue, fallback="remote")
    claimed = await queue.claim(EXECUTOR, ["remote"], live_models=["remote"])
    assert claimed.id == job.id
    assert claimed.model == "remote"
    assert claimed.params[S.PARAM_ORIGINAL_MODEL] == MODEL


async def test_normal_claim_no_model_swap(queue):
    job = await submit(queue)
    claimed = await queue.claim(EXECUTOR, [MODEL])
    assert claimed.id == job.id
    assert claimed.model == MODEL
    assert S.PARAM_ORIGINAL_MODEL not in claimed.params


async def test_pending_work_false_when_waiting_on_running_dependency(queue):
    a = await submit(queue)
    await submit(queue, depends_on=[a.id])
    await queue.claim(EXECUTOR, [MODEL])
    assert await queue.pending_work() is False


async def test_submit_depends_on_deduplicates_preserving_order(queue):
    a = await submit(queue)
    b = await submit(queue, depends_on=[a.id, a.id, a.id])
    assert b.params.get(S.PARAM_DEPENDS_ON) == [a.id]


async def test_submit_rejects_bad_purpose(queue):
    with pytest.raises(HubError, match="назначение"):
        await queue.submit(CLIENT, PROJECT, MODEL, TASK, purpose="other")


@pytest.mark.parametrize(
    "num_deps, base_branch, expect_error, check_base",
    [
        (0, None, False, lambda b: b.startswith("base/")),
        (0, "base/custom", False, lambda b: b == "base/custom"),
        (0, "agent/abc123", False, lambda b: b == "agent/abc123"),
        (1, None, False, lambda b: b.startswith("agent/") and len(b) > len("agent/")),
        (1, "base/x", False, lambda b: b == "base/x"),
        (2, None, True, None),
        (0, "agent/", True, None),
        (0, "agent/a..b", True, None),
        (0, "other/x", True, None),
    ],
)
async def test_submit_base_branch_from_dependency(
    queue, num_deps, base_branch, expect_error, check_base
):
    dep_jobs = []
    for _ in range(num_deps):
        dep_jobs.append(await submit(queue))
    dep_ids = [j.id for j in dep_jobs]

    kwargs = {}
    if base_branch is not None:
        kwargs["base_branch"] = base_branch
    if dep_ids:
        kwargs["depends_on"] = dep_ids

    if expect_error:
        with pytest.raises(HubError):
            await queue.submit(CLIENT, PROJECT, MODEL, TASK, **kwargs)
    else:
        job = await queue.submit(CLIENT, PROJECT, MODEL, TASK, **kwargs)
        assert check_base(job.base_branch)


async def test_claim_filters_by_models(queue):
    remote = await submit(queue, model=OTHER_MODEL, priority=9)
    local = await submit(queue)
    assert await queue.claim(EXECUTOR, []) is None
    claimed = await queue.claim(EXECUTOR, [MODEL])
    assert claimed.id == local.id
    assert claimed.executor == EXECUTOR
    assert claimed.started is not None
    assert (await queue.claim(EXECUTOR, [MODEL])) is None
    assert (await queue.claim(EXECUTOR, [OTHER_MODEL])).id == remote.id


async def test_question_answer_cycle_and_inbox_cursor(queue):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, S.MSG_STARTED)
    question = await queue.post_from_executor(job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "как?"})
    answer = await queue.post_from_client(CLIENT, job.id, S.MSG_ANSWER, {S.PAYLOAD_TEXT: "так"})
    await queue.post_from_executor(job.id, S.MSG_DONE)
    inbox = await queue.inbox(CLIENT)
    assert [m.kind for m in inbox] == [S.MSG_STARTED, S.MSG_QUESTION, S.MSG_DONE]
    assert inbox[1].payload == {S.PAYLOAD_TEXT: "как?"}
    assert [m.kind for m in await queue.inbox(CLIENT, since=question.seq)] == [S.MSG_DONE]
    assert answer.direction == S.DIRECTION_TO_EXECUTOR
    assert answer.seq > question.seq


async def test_inbox_isolated_between_clients_and_ack(queue):
    mine = await running_job(queue)
    await submit(queue, client=OTHER_CLIENT)
    other = await queue.claim(EXECUTOR, [MODEL])
    await queue.post_from_executor(mine.id, S.MSG_STARTED)
    await queue.post_from_executor(other.id, S.MSG_STARTED)
    step = await queue.post_from_executor(mine.id, S.MSG_STEP_DONE)
    assert {m.job_id for m in await queue.inbox(CLIENT)} == {mine.id}
    assert await queue.ack(OTHER_CLIENT, step.seq) == 1
    assert await queue.ack(CLIENT, step.seq) == 2
    assert await queue.ack(CLIENT, step.seq) == 0
    assert all(m.read for m in await queue.inbox(CLIENT))
    with pytest.raises(HubError):
        await queue.post_from_client(OTHER_CLIENT, mine.id, S.MSG_STOP)


async def test_inbox_all_spans_clients_and_keeps_read_state(queue):
    assert await queue.max_seq() == 0
    mine = await running_job(queue)
    await submit(queue, client=OTHER_CLIENT)
    other = await queue.claim(EXECUTOR, [MODEL])
    first = await queue.post_from_executor(mine.id, S.MSG_STARTED)
    await queue.post_from_executor(other.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
    last = await queue.post_from_client(OTHER_CLIENT, other.id, S.MSG_ANSWER, {S.PAYLOAD_TEXT: "a"})
    pairs = await queue.inbox_all()
    assert [(m.job_id, j.client, j.status) for m, j in pairs] == [
        (mine.id, CLIENT, S.STATUS_RUNNING),
        (other.id, OTHER_CLIENT, S.STATUS_RUNNING),
    ]
    assert [m.seq for m, _ in await queue.inbox_all(since=first.seq)] == [pairs[1][0].seq]
    assert len(await queue.inbox_all(limit=1)) == 1
    assert await queue.max_seq() == last.seq
    assert not any(m.read for m, _ in await queue.inbox_all())


async def test_poll_by_job_and_executor_marks_read(queue):
    job = await waiting_job(queue)
    answer = await queue.post_from_client(CLIENT, job.id, S.MSG_ANSWER)
    assert [m.seq for m in await queue.poll(job_id=job.id)] == [answer.seq]
    assert [m.seq for m in await queue.poll(executor=EXECUTOR)] == [answer.seq]
    assert await queue.poll(job_id=job.id, since=answer.seq) == []
    assert (await queue.poll(job_id=job.id))[0].read
    with pytest.raises(HubError):
        await queue.poll()


@pytest.mark.parametrize(
    ("setup_job", "expect_stop", "expect_resume"),
    [
        ("stop_then_resume", False, True),
        ("stop_only", True, False),
    ],
)
async def test_poll_filters_stale_stop_when_resume_follows(
    queue, setup_job, expect_stop, expect_resume
):
    if setup_job == "stop_then_resume":
        job = await running_job(queue)
        await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)
        await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME)
    else:
        job = await running_job(queue)
        await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)

    kinds = [m.kind for m in await queue.poll(executor=EXECUTOR)]
    if expect_stop:
        assert S.MSG_STOP in kinds
    else:
        assert S.MSG_STOP not in kinds
    if expect_resume:
        assert S.MSG_RESUME in kinds
    else:
        assert S.MSG_RESUME not in kinds


async def test_poll_stop_not_filtered_when_resume_then_later_stop(queue):
    job = await running_job(queue)
    await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)
    await queue.post_from_executor(job.id, S.MSG_STOPPED, executor=EXECUTOR)
    await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME)
    await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)

    kinds = [m.kind for m in await queue.poll(executor=EXECUTOR)]
    assert S.MSG_STOP in kinds
    assert S.MSG_RESUME in kinds


async def test_stop_queued_notifies_client_without_executor(queue):
    job = await submit(queue)
    await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)
    assert [m.kind for m in await queue.inbox(CLIENT)] == [S.MSG_STOPPED]
    assert (await queue.get_job(job.id)).finished is not None


async def test_stop_running_goes_to_executor_and_confirmation_is_recorded(queue):
    job = await running_job(queue)
    await queue.post_from_client(CLIENT, job.id, S.MSG_STOP)
    assert [m.kind for m in await queue.poll(job_id=job.id)] == [S.MSG_STOP]
    await queue.post_from_executor(job.id, S.MSG_STOPPED, executor=EXECUTOR)
    assert [m.kind for m in await queue.inbox(CLIENT)] == [S.MSG_STOPPED]


async def test_executor_identity_checked(queue):
    job = await running_job(queue)
    with pytest.raises(HubError):
        await queue.post_from_executor(job.id, S.MSG_STARTED, executor="чужой")


async def test_resume_with_other_model(queue):
    job = await finished_job(queue, S.MSG_FAILED)
    await queue.post_from_client(
        CLIENT, job.id, S.MSG_RESUME, {S.PAYLOAD_MODEL: OTHER_MODEL, S.PAYLOAD_TEXT: "замечание"}
    )
    resumed = await queue.get_job(job.id)
    assert (resumed.status, resumed.model, resumed.finished) == (S.STATUS_QUEUED, OTHER_MODEL, None)
    assert await queue.claim(EXECUTOR, [MODEL]) is None
    assert (await queue.claim(EXECUTOR, [OTHER_MODEL])).id == job.id


async def test_resume_without_model_keeps_it(queue):
    job = await waiting_job(queue)
    await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME)
    assert (await queue.get_job(job.id)).model == MODEL


@pytest.mark.parametrize("payload", [{S.PAYLOAD_MODEL: ""}, {S.PAYLOAD_MODEL: 5}])
async def test_resume_rejects_bad_model(queue, payload):
    job = await finished_job(queue, S.MSG_FAILED)
    with pytest.raises(HubError):
        await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME, payload)
    assert (await queue.get_job(job.id)).status == S.STATUS_FAILED


async def test_release_only_while_waiting_owner(queue):
    job = await running_job(queue)
    with pytest.raises(HubError):
        await queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE)
    await queue.post_from_executor(job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
    await queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE)
    assert (await queue.get_job(job.id)).status == S.STATUS_WAITING_OWNER
    await queue.post_from_executor(job.id, S.MSG_FAILED)
    with pytest.raises(HubError):
        await queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE)


async def test_spend_is_summed_over_messages(queue):
    job = await running_job(queue)
    await queue.post_from_executor(
        job.id, S.MSG_STEP_DONE, {S.PAYLOAD_TOKENS: 100, S.PAYLOAD_COST_USD: 0.5}
    )
    await queue.post_from_executor(job.id, S.MSG_STEP_DONE)
    await queue.post_from_executor(
        job.id, S.MSG_DONE, {S.PAYLOAD_TOKENS: 50, S.PAYLOAD_COST_USD: 0.25}
    )
    done = await queue.get_job(job.id)
    assert (done.tokens, done.cost_usd) == (150, 0.75)


@pytest.mark.parametrize(
    "payload", [{S.PAYLOAD_TOKENS: -1}, {S.PAYLOAD_COST_USD: "x"}, {S.PAYLOAD_TOKENS: True}]
)
async def test_bad_spend_rejected_without_side_effects(queue, payload):
    job = await running_job(queue)
    with pytest.raises(HubError):
        await queue.post_from_executor(job.id, S.MSG_DONE, payload)
    assert (await queue.get_job(job.id)).status == S.STATUS_RUNNING
    assert await queue.inbox(CLIENT) == []


async def test_purge_deletes_only_old_read_messages(queue):
    job = await running_job(queue)
    first = await queue.post_from_executor(job.id, S.MSG_STARTED)
    await queue.post_from_executor(job.id, S.MSG_STEP_DONE)
    await queue.ack(CLIENT, first.seq)
    assert await queue.purge(30) == 0
    future = utc_now() + timedelta(days=31)
    assert await queue.purge(30, now=future) == 1
    assert [m.kind for m in await queue.inbox(CLIENT)] == [S.MSG_STEP_DONE]
    assert await queue.purge(30, now=future + timedelta(days=365)) == 0


async def test_list_jobs_filters(queue):
    running = await running_job(queue)
    queued = await submit(queue)
    other = await submit(queue, client=OTHER_CLIENT)
    assert {j.id for j in await queue.list_jobs()} == {queued.id, running.id, other.id}
    assert {j.id for j in await queue.list_jobs(client=CLIENT)} == {queued.id, running.id}
    assert [j.id for j in await queue.list_jobs(statuses=[S.STATUS_RUNNING])] == [running.id]


async def submit_test(queue, client=CLIENT, **overrides):
    return await queue.submit_test(client, PROJECT, **{"ref": "test/c/1", **overrides})


def done_payload(**overrides):
    return {
        S.PAYLOAD_EXIT_CODE: 1,
        S.PAYLOAD_DURATION_S: 12.5,
        S.PAYLOAD_SUMMARY: {
            S.SUMMARY_PASSED: 9,
            S.SUMMARY_FAILED: 1,
            S.SUMMARY_ERRORS: 0,
            S.SUMMARY_SKIPPED: 2,
        },
        S.PAYLOAD_FAILURES: [{"name": "t::a", "excerpt": "assert 1 == 2"}],
        S.PAYLOAD_TAIL: "1 failed",
        **overrides,
    }


async def test_submit_test_stores_params(queue):
    job = await submit_test(queue, cmd=" pytest ", args=["-x", "tests/a.py"])
    assert job.kind == S.JOB_KIND_TEST
    assert job.params == {
        S.FIELD_REF: "test/c/1",
        S.FIELD_CMD: "pytest",
        S.FIELD_ARGS: ["-x", "tests/a.py"],
    }
    assert (await submit_test(queue)).params[S.FIELD_CMD] == ""


async def test_submit_test_depends_on_blocks_until_done(queue):
    a = await submit(queue)
    test_job = await queue.submit_test(CLIENT, PROJECT, "test/c/1", depends_on=[a.id])
    first = await queue.claim(EXECUTOR, [MODEL], kinds=(S.JOB_KIND_TEST, S.JOB_KIND_AGENT))
    assert first.id == a.id
    assert await queue.claim(EXECUTOR, [MODEL], kinds=(S.JOB_KIND_TEST, S.JOB_KIND_AGENT)) is None
    await queue.post_from_executor(a.id, S.MSG_DONE)
    second = await queue.claim(EXECUTOR, [MODEL], kinds=(S.JOB_KIND_TEST, S.JOB_KIND_AGENT))
    assert second.id == test_job.id


async def test_submit_test_ref_from_single_dependency(queue):
    dep = await submit(queue)
    job = await queue.submit_test(CLIENT, PROJECT, "", depends_on=[dep.id])
    assert job.params[S.FIELD_REF] == S.AGENT_BRANCH_PREFIX + dep.id


@pytest.mark.parametrize("deps", [[], ["nonexistent"]])
async def test_submit_test_empty_ref_needs_one_known_dependency(queue, deps):
    with pytest.raises(HubError):
        await queue.submit_test(CLIENT, PROJECT, "", depends_on=deps)


@pytest.mark.parametrize("ref", ["", " ", "-x", "a b"])
async def test_submit_test_rejects_bad_ref(queue, ref):
    with pytest.raises(HubError):
        await submit_test(queue, ref=ref)


async def test_submit_test_unknown_project(queue):
    with pytest.raises(HubError):
        await queue.submit_test(CLIENT, "нет", "master")


async def test_test_jobs_go_before_agent_jobs_whatever_priority(queue):
    await submit(queue, priority=50)
    test_job = await submit_test(queue)
    claimed = await queue.claim(EXECUTOR, [MODEL], [S.JOB_KIND_AGENT, S.JOB_KIND_TEST])
    assert claimed.id == test_job.id


async def test_claim_by_kinds_is_separate(queue):
    agent = await submit(queue)
    test_job = await submit_test(queue)
    assert await queue.claim(EXECUTOR, [MODEL], [S.JOB_KIND_TEST]) is not None
    assert await queue.claim(EXECUTOR, [MODEL], [S.JOB_KIND_TEST]) is None
    assert await queue.claim(EXECUTOR, [], [S.JOB_KIND_AGENT]) is None
    assert (await queue.claim(EXECUTOR, [MODEL])).id == agent.id
    assert (await queue.get_job(test_job.id)).status == S.STATUS_RUNNING


async def test_test_claim_needs_no_models(queue):
    job = await submit_test(queue)
    assert (await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])).id == job.id


async def test_claim_unknown_kind_is_rejected(queue):
    with pytest.raises(HubError):
        await queue.claim(EXECUTOR, [MODEL], ["nope"])


async def test_test_done_finishes_job_and_keeps_result(queue):
    job = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(job.id, S.MSG_TEST_DONE, done_payload(), executor=EXECUTOR)
    assert (await queue.get_job(job.id)).status == S.STATUS_DONE
    message = await queue.last_message(job.id, S.MSG_TEST_DONE)
    assert message.payload[S.PAYLOAD_EXIT_CODE] == 1
    assert [m.kind for m in await queue.inbox(CLIENT)] == [S.MSG_TEST_DONE]


async def test_fix_task_text_lists_failures_and_tail():
    text = fix_task_text("base/x", done_payload())
    assert text == (
        "# fix: красный прогон base/x\n"
        "\n"
        "- t::a\n"
        "  assert 1 == 2\n"
        "```\n"
        "1 failed\n"
        "```\n"
        "\n"
        "Почини упавшие тесты минимальной правкой, не меняя смысла тестов; прогони их и `checks`."
    )


async def test_fix_task_text_without_failures():
    text = fix_task_text(
        "agent/x1",
        done_payload(**{S.PAYLOAD_FAILURES: [], S.PAYLOAD_TAIL: "хвост лога"}),
    )
    assert "- t::a" not in text
    assert "```\nхвост лога\n```" in text
    assert text.endswith(
        "Почини упавшие тесты минимальной правкой, не меняя смысла тестов; прогони их и `checks`."
    )


async def test_submit_test_stores_fix_params(queue):
    job = await queue.submit_test(CLIENT, PROJECT, "base/x", fix_model=MODEL, fix_round=1)
    assert job.params[S.PARAM_FIX_MODEL] == MODEL
    assert job.params[S.PARAM_FIX_ROUND] == 1
    plain = await submit_test(queue)
    assert S.PARAM_FIX_MODEL not in plain.params
    assert S.PARAM_FIX_ROUND not in plain.params


async def test_submit_test_fix_model_with_agent_ref(queue):
    dep = await submit(queue)
    job = await queue.submit_test(CLIENT, PROJECT, S.AGENT_BRANCH_PREFIX + dep.id, fix_model=MODEL)
    assert job.params[S.PARAM_FIX_MODEL] == MODEL


@pytest.mark.parametrize("ref", ["test/alice/1", "1234567890a"])
async def test_submit_test_fix_model_needs_branch_ref(queue, ref):
    with pytest.raises(HubError, match="base/<имя> или agent/<id>"):
        await queue.submit_test(CLIENT, PROJECT, ref, fix_model=MODEL)


async def test_red_test_run_with_fix_model_creates_fix_job(queue):
    dep = await submit(queue)
    ref = S.AGENT_BRANCH_PREFIX + dep.id
    test_job = await queue.submit_test(CLIENT, PROJECT, ref, fix_model=MODEL)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(test_job.id, S.MSG_TEST_DONE, done_payload())
    fix_jobs = [
        j
        for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])
        if j.params.get(S.PARAM_PURPOSE) == S.PURPOSE_FIX
    ]
    assert len(fix_jobs) == 1
    fix = fix_jobs[0]
    assert fix.client == CLIENT
    assert fix.project == PROJECT
    assert fix.model == MODEL
    assert fix.base_branch == ref
    assert fix.params[S.PARAM_PROFILE] == S.FIX_PROFILE
    assert fix.params[S.PARAM_FIX_OF] == test_job.id
    assert fix.params[S.PARAM_FIX_ROUND] == 1
    assert fix.params[S.PARAM_FIX_MODEL] == MODEL
    assert fix.task.startswith(f"# fix: красный прогон {ref}")
    assert "t::a" in fix.task


async def test_green_test_run_with_fix_model_creates_no_fix_job(queue):
    test_job = await queue.submit_test(CLIENT, PROJECT, "base/x", fix_model=MODEL)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(
        test_job.id, S.MSG_TEST_DONE, done_payload(**{S.PAYLOAD_EXIT_CODE: 0})
    )
    assert [j.kind for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])] == []


async def test_red_test_run_without_fix_model_creates_no_fix_job(queue):
    test_job = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(test_job.id, S.MSG_TEST_DONE, done_payload())
    assert [j.kind for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])] == []


async def test_red_test_run_at_fix_round_limit_creates_no_fix_job(queue):
    test_job = await queue.submit_test(
        CLIENT, PROJECT, "base/x", fix_model=MODEL, fix_round=S.FIX_MAX_ROUNDS
    )
    assert test_job.params[S.PARAM_FIX_ROUND] == S.FIX_MAX_ROUNDS
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(test_job.id, S.MSG_TEST_DONE, done_payload())
    assert [j.kind for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])] == []


async def test_fix_done_creates_retest_job(queue):
    dep = await submit(queue)
    ref = S.AGENT_BRANCH_PREFIX + dep.id
    test_job = await queue.submit_test(CLIENT, PROJECT, ref, fix_model=MODEL)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(test_job.id, S.MSG_TEST_DONE, done_payload())
    fix_jobs = [
        j
        for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])
        if j.params.get(S.PARAM_PURPOSE) == S.PURPOSE_FIX
    ]
    assert len(fix_jobs) == 1
    fix = fix_jobs[0]
    await queue.claim(EXECUTOR, [MODEL], kinds=(S.JOB_KIND_AGENT,))
    await queue.post_from_executor(fix.id, S.MSG_DONE)
    test_jobs = [
        j for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED]) if j.kind == S.JOB_KIND_TEST
    ]
    assert len(test_jobs) == 1
    retest = test_jobs[0]
    assert retest.client == CLIENT
    assert retest.project == PROJECT
    assert retest.params[S.FIELD_REF] == S.AGENT_BRANCH_PREFIX + fix.id
    assert retest.params[S.FIELD_CMD] == ""
    assert retest.params[S.FIELD_ARGS] == []
    assert retest.params[S.PARAM_FIX_MODEL] == fix.params[S.PARAM_FIX_MODEL]
    assert retest.params[S.PARAM_FIX_ROUND] == fix.params[S.PARAM_FIX_ROUND]


async def test_dev_done_creates_no_retest_job(queue):
    job = await submit(queue)
    await queue.claim(EXECUTOR, [MODEL])
    await queue.post_from_executor(job.id, S.MSG_DONE)
    assert [j.kind for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])] == []


async def test_fix_failed_creates_no_retest_job(queue):
    fix = await submit(queue, purpose=S.PURPOSE_FIX)
    await queue.claim(EXECUTOR, [MODEL])
    await queue.post_from_executor(fix.id, S.MSG_FAILED, {S.PAYLOAD_TEXT: "сломалось"})
    assert [j.kind for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])] == []


async def test_red_test_run_at_fix_round_limit_sends_fix_exhausted(queue):
    test_job = await queue.submit_test(
        CLIENT, PROJECT, "base/x", fix_model=MODEL, fix_round=S.FIX_MAX_ROUNDS
    )
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(test_job.id, S.MSG_TEST_DONE, done_payload())
    assert [j.kind for j in await queue.list_jobs(statuses=[S.STATUS_QUEUED])] == []
    inbox = await queue.inbox(CLIENT)
    assert [m.kind for m in inbox] == [S.MSG_FIX_EXHAUSTED, S.MSG_TEST_DONE]
    assert inbox[0].payload[S.PAYLOAD_TEXT] == (
        f"fix не справился за {S.FIX_MAX_ROUNDS} круга, нужен владелец: base/x"
    )


async def test_test_job_transitions(queue):
    stopped = await submit_test(queue)
    await queue.post_from_client(CLIENT, stopped.id, S.MSG_STOP, {})
    assert (await queue.get_job(stopped.id)).status == S.STATUS_STOPPED
    failed = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(failed.id, S.MSG_FAILED, {S.PAYLOAD_TEXT: "нет образа"})
    assert (await queue.get_job(failed.id)).status == S.STATUS_FAILED
    await queue.post_from_client(CLIENT, failed.id, S.MSG_RESUME, {})
    assert (await queue.get_job(failed.id)).status == S.STATUS_QUEUED


async def test_refused_transition_carries_current_status(queue):
    failed = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(failed.id, S.MSG_FAILED, {S.PAYLOAD_TEXT: "x"})
    with pytest.raises(TransitionRefused) as info:
        await queue.post_from_executor(failed.id, S.MSG_FAILED, {S.PAYLOAD_TEXT: "x"})
    assert info.value.status == S.STATUS_FAILED


async def test_test_job_rejects_question_and_agent_rejects_test_done(queue):
    test_job = await submit_test(queue)
    agent = await running_job(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    with pytest.raises(HubError):
        await queue.post_from_executor(test_job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
    with pytest.raises(HubError):
        await queue.post_from_executor(agent.id, S.MSG_TEST_DONE, done_payload())
    assert (await queue.get_job(test_job.id)).status == S.STATUS_RUNNING


@pytest.mark.parametrize(
    "bad",
    [
        {S.PAYLOAD_EXIT_CODE: "0"},
        {S.PAYLOAD_EXIT_CODE: True},
        {S.PAYLOAD_DURATION_S: -1},
        {S.PAYLOAD_SUMMARY: {S.SUMMARY_PASSED: 1}},
        {S.PAYLOAD_SUMMARY: "ok"},
        {S.PAYLOAD_FAILURES: "x"},
        {S.PAYLOAD_TAIL: 5},
    ],
)
async def test_test_done_payload_is_validated(queue, bad):
    job = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    with pytest.raises(HubError):
        await queue.post_from_executor(job.id, S.MSG_TEST_DONE, done_payload(**bad))
    assert (await queue.get_job(job.id)).status == S.STATUS_RUNNING


async def test_pending_work_counts_queued_test(queue):
    assert await queue.pending_work() is False
    job = await submit_test(queue)
    assert await queue.pending_work() is True
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    assert await queue.pending_work() is False
    await queue.post_from_executor(job.id, S.MSG_TEST_DONE, done_payload())
    assert await queue.pending_work() is False


async def test_wait_terminal_wakes_on_finish(queue):
    job = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    waiter = asyncio.create_task(queue.wait_terminal(job.id, 30))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await queue.post_from_executor(job.id, S.MSG_TEST_DONE, done_payload())
    assert (await asyncio.wait_for(waiter, 1)).status == S.STATUS_DONE


async def test_wait_terminal_times_out_with_current_job(queue):
    job = await submit_test(queue)
    result = await queue.wait_terminal(job.id, 0.05)
    assert result.status == S.STATUS_QUEUED


async def test_wait_terminal_returns_at_once_for_finished(queue):
    job = await submit_test(queue)
    await queue.post_from_client(CLIENT, job.id, S.MSG_STOP, {})
    assert (await asyncio.wait_for(queue.wait_terminal(job.id, 30), 1)).status == S.STATUS_STOPPED


async def test_purge_removes_logs_of_old_test_jobs_only(queue):
    old = await submit_test(queue)
    fresh = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    for job in (old, fresh):
        directory = queue.logs_root / job.id
        directory.mkdir(parents=True)
        (directory / S.LOG_FILE_OUTPUT).write_text("x")
        await queue.post_from_executor(job.id, S.MSG_TEST_DONE, done_payload())
    await queue.purge(5, now=utc_now() + timedelta(days=6))
    assert not (queue.logs_root / old.id).exists()
    assert not (queue.logs_root / fresh.id).exists()
    third = await submit_test(queue)
    (queue.logs_root / third.id).mkdir(parents=True)
    await queue.purge(5)
    assert (queue.logs_root / third.id).exists()


async def test_open_migrates_jobs_without_params(tmp_path):
    schema_v0152 = S_QUEUE_SCHEMA.replace(",\n    params TEXT NOT NULL DEFAULT '{}'", "")
    assert "params" not in schema_v0152
    with sqlite3.connect(tmp_path / HUB_DB_NAME) as old:
        old.executescript(schema_v0152)
        old.execute("INSERT INTO projects VALUES (?, '2026-11-01T00:00:00+00:00')", (PROJECT,))
        old.execute(
            "INSERT INTO jobs (id, kind, project, client, model, task, base_branch, status,"
            " created) VALUES ('old111', 'agent', ?, ?, ?, 't', 'base/x', 'queued',"
            " '2026-11-01T00:00:00+00:00')",
            (PROJECT, CLIENT, MODEL),
        )
    store = HubStore(tmp_path)
    await store.open()
    try:
        assert await store.queue.projects() == [PROJECT]
        assert (await store.queue.get_job("old111")).params == {}
        test_job = await store.queue.submit_test(CLIENT, PROJECT, "master", args=["-x"])
        claimed = await store.queue.claim(EXECUTOR, [MODEL], [S.JOB_KIND_TEST])
        assert claimed.id == test_job.id
        assert claimed.params[S.FIELD_ARGS] == ["-x"]
        assert (await store.queue.claim(EXECUTOR, [MODEL])).id == "old111"
    finally:
        await store.close()
    reopened = HubStore(tmp_path)
    await reopened.open()
    await reopened.close()


STALL_S = 1800.0


def stalled_messages(queue_messages):
    return [m for m in queue_messages if m.kind == S.MSG_STALLED]


async def test_flag_stalled_after_threshold_once(queue):
    job = await running_job(queue)
    now = utc_now()
    assert await queue.flag_stalled(STALL_S, True, now + timedelta(seconds=STALL_S - 60)) == []
    first = await queue.flag_stalled(STALL_S, False, now + timedelta(seconds=STALL_S + 60))
    assert [m.job_id for m in first] == [job.id]
    assert first[0].kind == S.MSG_STALLED
    assert first[0].payload == {S.PAYLOAD_SILENT_MIN: 31, S.PAYLOAD_EXECUTOR_ALIVE: False}
    assert await queue.flag_stalled(STALL_S, True, now + timedelta(seconds=STALL_S * 3)) == []
    inbox = await queue.inbox(CLIENT)
    assert len(stalled_messages(inbox)) == 1


async def test_flag_stalled_again_after_news(queue):
    job = await running_job(queue)
    await queue.flag_stalled(STALL_S, True, utc_now() + timedelta(seconds=STALL_S + 60))
    await queue.post_from_executor(job.id, S.MSG_STEP_DONE)
    later = utc_now() + timedelta(seconds=STALL_S * 2)
    again = await queue.flag_stalled(STALL_S, True, later)
    assert len(again) == 1
    assert len(stalled_messages(await queue.inbox(CLIENT))) == 2


async def test_flag_stalled_step_done_resets_clock(queue):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, S.MSG_STEP_DONE)
    soon = utc_now() + timedelta(seconds=STALL_S - 60)
    assert await queue.flag_stalled(STALL_S, True, soon) == []


async def test_flag_stalled_ignores_other_states_and_kinds(queue):
    await waiting_job(queue)
    await submit(queue, model=OTHER_MODEL)
    await queue.submit_test(CLIENT, PROJECT, "master")
    far = utc_now() + timedelta(days=2)
    assert await queue.flag_stalled(STALL_S, True, far) == []


@pytest.mark.parametrize(
    "executor_alive, live_models, fallback, expected_kind",
    [
        (False, [MODEL], "", S.MSG_EXECUTOR_OFFLINE),
        (True, [], "", S.MSG_WAITING_MODEL),
        (True, [], "remote", S.MSG_WAITING_MODEL),
        (True, ["remote"], "remote", None),
        (True, [MODEL], "", None),
        (True, [OTHER_MODEL], "", S.MSG_WAITING_MODEL),
    ],
)
async def test_flag_waiting(queue, executor_alive, live_models, fallback, expected_kind):
    await submit(queue, fallback=fallback)
    now = utc_now()
    far = now + timedelta(seconds=STALL_S + 60)
    messages = await queue.flag_waiting(STALL_S, executor_alive, live_models, far)
    if expected_kind is None:
        assert messages == []
    else:
        assert len(messages) == 1
        assert messages[0].kind == expected_kind
        assert messages[0].payload == {S.PAYLOAD_WAITED_MIN: 31}
        again = await queue.flag_waiting(STALL_S, executor_alive, live_models, far)
        assert again == []


async def test_flag_waiting_skips_unready_dependency(queue):
    a = await submit(queue)
    await submit(queue, depends_on=[a.id])
    await queue.claim(EXECUTOR, [MODEL])
    now = utc_now()
    far = now + timedelta(seconds=STALL_S + 60)
    messages = await queue.flag_waiting(STALL_S, True, [], far)
    assert messages == []


async def test_flag_waiting_skips_young_job(queue):
    await submit(queue)
    now = utc_now()
    soon = now + timedelta(seconds=STALL_S - 60)
    messages = await queue.flag_waiting(STALL_S, True, [], soon)
    assert messages == []


@pytest.mark.parametrize(
    ("status", "models", "expected"),
    [
        ("queued", {MODEL}, True),
        ("running", {MODEL}, True),
        ("waiting_owner", {MODEL}, False),
        ("done", {MODEL}, False),
        ("queued", {OTHER_MODEL}, False),
        ("queued", set(), False),
    ],
    ids=["queued", "running", "waiting_owner", "done", "чужая модель", "пустой набор"],
)
async def test_local_demand_matches_active_jobs_with_model(queue, status, models, expected):
    await SETUPS[status](queue)
    assert await queue.local_demand(models) is expected


async def test_progress_change_counts_as_news(queue):
    job = await running_job(queue)
    start = utc_now()
    await queue.note_progress({job.id: 1}, start + timedelta(seconds=STALL_S - 100))
    assert await queue.flag_stalled(STALL_S, True, start + timedelta(seconds=STALL_S + 60)) == []
    await queue.note_progress({job.id: 1}, start + timedelta(seconds=STALL_S + 50))
    assert await queue.flag_stalled(STALL_S, True, start + timedelta(seconds=STALL_S + 60)) == []
    late = start + timedelta(seconds=STALL_S * 2)
    assert len(await queue.flag_stalled(STALL_S, True, late)) == 1


async def test_progress_ignores_unknown_jobs_and_junk(queue):
    await queue.note_progress({"nope": 3, "x": True, "y": "1"})
    await queue.note_progress("junk")
    await queue.note_progress(None)


async def claimed_test(queue):
    job = await submit_test(queue)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    return job


async def test_requeue_returns_test_job_to_queue(queue):
    job = await claimed_test(queue)
    await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor=EXECUTOR)
    after = await queue.get_job(job.id)
    assert (after.status, after.executor, after.finished) == (S.STATUS_QUEUED, None, None)
    assert after.params[S.PARAM_ATTEMPTS] == 1
    assert [m.kind for m in await queue.inbox(CLIENT)] == [S.MSG_REQUEUE]
    again = await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    assert again.id == job.id


async def test_requeue_does_not_wake_terminal_waiter(queue):
    job = await claimed_test(queue)
    waiting = asyncio.create_task(queue.wait_terminal(job.id, 5))
    await asyncio.sleep(0)
    await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor=EXECUTOR)
    await asyncio.sleep(0)
    assert not waiting.done()
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(job.id, S.MSG_TEST_DONE, done_payload(), executor=EXECUTOR)
    assert (await waiting).status == S.STATUS_DONE


async def claimed_job(queue, kind):
    if kind == S.JOB_KIND_TEST:
        return await claimed_test(queue)
    job = await submit(queue)
    await queue.claim(EXECUTOR, [MODEL])
    return job


@pytest.mark.parametrize(
    ("kind", "limit", "phrase"),
    [
        (S.JOB_KIND_TEST, S.TEST_MAX_REQUEUES, "перезапускался"),
        (S.JOB_KIND_AGENT, S.AGENT_MAX_REQUEUES, "возвращалось"),
    ],
    ids=("test", "agent"),
)
async def test_requeue_limit_fails_job_with_reason(queue, kind, limit, phrase):
    job = await claimed_job(queue, kind)
    for _ in range(limit):
        await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor=EXECUTOR)
        if kind == S.JOB_KIND_TEST:
            await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
        else:
            await queue.claim(EXECUTOR, [MODEL])
    await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor=EXECUTOR)
    after = await queue.get_job(job.id)
    assert after.status == S.STATUS_FAILED
    message = await queue.last_message(job.id, S.MSG_FAILED)
    assert str(limit + 1) in message.payload[S.PAYLOAD_TEXT]
    assert phrase in message.payload[S.PAYLOAD_TEXT]


async def test_requeue_returns_agent_job_to_queue(queue):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor=EXECUTOR)
    after = await queue.get_job(job.id)
    assert (after.status, after.executor, after.finished) == (S.STATUS_QUEUED, None, None)
    assert after.params[S.PARAM_ATTEMPTS] == 1
    assert [m.kind for m in await queue.inbox(CLIENT)] == [S.MSG_REQUEUE]
    again = await queue.claim(EXECUTOR, [MODEL])
    assert again.id == job.id


async def test_requeue_rejected_when_not_running(queue):
    done = await claimed_test(queue)
    await queue.post_from_executor(done.id, S.MSG_TEST_DONE, done_payload(), executor=EXECUTOR)
    with pytest.raises(HubError):
        await queue.post_from_executor(done.id, S.MSG_REQUEUE, executor=EXECUTOR)
    assert (await queue.get_job(done.id)).status == S.STATUS_DONE
    queued = await submit_test(queue)
    with pytest.raises(HubError):
        await queue.post_from_executor(queued.id, S.MSG_REQUEUE)


async def test_requeue_rejected_for_foreign_executor(queue):
    job = await claimed_test(queue)
    with pytest.raises(HubError):
        await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor="чужой")
    assert (await queue.get_job(job.id)).status == S.STATUS_RUNNING


async def test_resume_resets_requeue_attempts(queue):
    job = await claimed_test(queue)
    await queue.post_from_executor(job.id, S.MSG_REQUEUE, executor=EXECUTOR)
    await queue.claim(EXECUTOR, [], [S.JOB_KIND_TEST])
    await queue.post_from_executor(job.id, S.MSG_FAILED, {S.PAYLOAD_TEXT: "x"})
    await queue.post_from_client(CLIENT, job.id, S.MSG_RESUME, {})
    assert S.PARAM_ATTEMPTS not in (await queue.get_job(job.id)).params


async def test_active_jobs_order_and_paging(queue):
    running = await running_job(queue)
    waiting = await submit(queue)
    await queue.claim(EXECUTOR, [MODEL])
    await queue.post_from_executor(waiting.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
    done = await submit(queue)
    await queue.claim(EXECUTOR, [MODEL])
    await queue.post_from_executor(done.id, S.MSG_DONE)
    fix = await submit(queue, purpose=S.PURPOSE_FIX)
    dev = await submit(queue, priority=5)
    bench = await submit(queue, purpose=S.PURPOSE_BENCH)
    jobs, total = await queue.active_jobs(0, 10)
    assert total == 5
    assert [j.id for j in jobs] == [running.id, waiting.id, fix.id, dev.id, bench.id]
    page, total = await queue.active_jobs(2, 2)
    assert [j.id for j in page] == [fix.id, dev.id]
    assert total == 5


async def test_step_stats_empty_list(queue):
    assert await queue.step_stats([]) == {}


async def test_step_stats_job_without_steps_absent(queue):
    job = await submit(queue)
    stats = await queue.step_stats([job.id])
    assert stats == {}


async def test_step_stats_two_steps_returns_count_and_last_text(queue):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, S.MSG_STEP_DONE, {S.PAYLOAD_TEXT: "первый шаг"})
    await queue.post_from_executor(job.id, S.MSG_STEP_DONE, {S.PAYLOAD_TEXT: "второй шаг"})
    stats = await queue.step_stats([job.id])
    assert stats == {job.id: (2, "второй шаг")}


async def test_step_stats_multiple_jobs(queue):
    job1 = await running_job(queue)
    await queue.post_from_executor(job1.id, S.MSG_STEP_DONE, {S.PAYLOAD_TEXT: "шаг 1"})
    job2 = await running_job(queue)
    await queue.post_from_executor(job2.id, S.MSG_STEP_DONE, {S.PAYLOAD_TEXT: "шаг 2"})
    await queue.post_from_executor(job2.id, S.MSG_STEP_DONE, {S.PAYLOAD_TEXT: "шаг 3"})
    job3 = await submit(queue)
    stats = await queue.step_stats([job1.id, job2.id, job3.id])
    assert stats == {job1.id: (1, "шаг 1"), job2.id: (2, "шаг 3")}


async def test_job_statuses_empty_list(queue):
    assert await queue.job_statuses([]) == {}


async def test_job_statuses_returns_status_map(queue):
    job1 = await submit(queue)
    job2 = await submit(queue)
    statuses = await queue.job_statuses([job1.id, job2.id, "nope"])
    assert statuses == {job1.id: S.STATUS_QUEUED, job2.id: S.STATUS_QUEUED}


async def test_slot_waiting_empty_list(queue):
    assert await queue.slot_waiting([]) == set()


@pytest.mark.parametrize(
    ("setup", "expected_waiting"),
    [
        ("answer_no_started", True),
        ("answer_then_started", False),
        ("no_answer", False),
    ],
    ids=["answer_no_started", "answer_then_started", "no_answer"],
)
async def test_slot_waiting_detects_waiting(queue, setup, expected_waiting):
    job = await running_job(queue)
    await queue.post_from_executor(job.id, S.MSG_STARTED)
    if setup != "no_answer":
        await queue.post_from_executor(job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "?"})
        await queue.post_from_client(CLIENT, job.id, S.MSG_ANSWER, {S.PAYLOAD_TEXT: "ans"})
    if setup == "answer_then_started":
        await queue.post_from_executor(job.id, S.MSG_STARTED)
    waiting = await queue.slot_waiting([job.id])
    if expected_waiting:
        assert waiting == {job.id}
    else:
        assert waiting == set()


async def test_add_review_unknown_job(queue):
    with pytest.raises(HubError, match="задания"):
        await queue.add_review(CLIENT, "nope", S.REVIEW_ACCEPTED, [])


@pytest.mark.parametrize(
    "bad_verdict",
    ["acceptedd", "reject", ""],
)
async def test_add_review_bad_verdict(queue, bad_verdict):
    job = await submit(queue)
    with pytest.raises(HubError, match="вердикт"):
        await queue.add_review(CLIENT, job.id, bad_verdict, [])


@pytest.mark.parametrize(
    "bad_tag",
    ["loopp", "missed", "unknown", ""],
)
async def test_add_review_bad_tag(queue, bad_tag):
    job = await submit(queue)
    issues = [{S.FIELD_TAG: bad_tag, S.FIELD_NOTE: "note"}]
    with pytest.raises(HubError, match="метка"):
        await queue.add_review(CLIENT, job.id, S.REVIEW_ACCEPTED, issues)


async def test_add_review_issue_not_dict(queue):
    job = await submit(queue)
    issues = ["not a dict"]
    with pytest.raises(HubError, match="issues"):
        await queue.add_review(CLIENT, job.id, S.REVIEW_ACCEPTED, issues)


async def test_add_review_missing_note(queue):
    job = await submit(queue)
    issues = [{S.FIELD_TAG: S.ISSUE_LOOP}]
    with pytest.raises(HubError, match="issues"):
        await queue.add_review(CLIENT, job.id, S.REVIEW_ACCEPTED, issues)


async def test_add_review_and_reviews_match(queue):
    job = await submit(queue)
    issues = [
        {S.FIELD_TAG: S.ISSUE_LOOP, S.FIELD_NOTE: "infinite loop"},
        {S.FIELD_TAG: S.ISSUE_MISSED_CASE, S.FIELD_NOTE: "edge case"},
    ]
    added = await queue.add_review(
        CLIENT,
        job.id,
        S.REVIEW_ACCEPTED_WITH_FIXES,
        issues,
        fix_commit="abc123",
        tests="pytest output",
    )
    all_reviews = await queue.reviews()
    assert len(all_reviews) == 1
    assert added == all_reviews[0]
    assert added[S.FIELD_ID] == job.id
    assert added[S.FIELD_MODEL] == MODEL
    assert added[S.PARAM_PROFILE] == S.DEFAULT_PROFILE
    assert added[S.FIELD_TITLE] == "цель"
    assert added[S.FIELD_BASE_BRANCH] == job.base_branch
    assert added[S.PAYLOAD_TOKENS] == 0
    assert added[S.FIELD_RESUMES] == 0
    assert added[S.FIELD_VERDICT] == S.REVIEW_ACCEPTED_WITH_FIXES
    assert added[S.FIELD_ISSUES] == issues
    assert added[S.FIELD_FIX_COMMIT] == "abc123"
    assert added[S.FIELD_TESTS] == "pytest output"
    assert added[S.FIELD_REVIEWED] is not None





async def test_add_review_replaces_previous(queue):
    job = await submit(queue)
    await queue.add_review(CLIENT, job.id, S.REVIEW_ACCEPTED, [])
    issues = [{S.FIELD_TAG: S.ISSUE_SCOPE, S.FIELD_NOTE: "out of scope"}]
    await queue.add_review(CLIENT, job.id, S.REVIEW_ABANDONED, issues)
    all_reviews = await queue.reviews()
    assert len(all_reviews) == 1
    assert all_reviews[0][S.FIELD_VERDICT] == S.REVIEW_ABANDONED
    assert all_reviews[0][S.FIELD_ISSUES] == issues


async def test_reviews_filter_by_model(queue):
    job1 = await submit(queue, model=MODEL)
    job2 = await submit(queue, model=OTHER_MODEL)
    await queue.add_review(CLIENT, job1.id, S.REVIEW_ACCEPTED, [])
    await queue.add_review(CLIENT, job2.id, S.REVIEW_ACCEPTED, [])
    filtered = await queue.reviews(model=MODEL)
    assert len(filtered) == 1
    assert filtered[0][S.FIELD_ID] == job1.id


async def test_reviews_filter_by_since(queue):
    job1 = await submit(queue)
    await queue.add_review(CLIENT, job1.id, S.REVIEW_ACCEPTED, [])
    import asyncio
    await asyncio.sleep(0.01)
    job2 = await submit(queue)
    await queue.add_review(CLIENT, job2.id, S.REVIEW_ACCEPTED, [])
    since = (await queue.reviews())[0][S.FIELD_REVIEWED]
    filtered = await queue.reviews(since=since)
    assert len(filtered) == 2
    await queue.reviews(since=since)
    import datetime
    future = (
        datetime.datetime.fromisoformat(since) + datetime.timedelta(seconds=1)
    ).isoformat()
    filtered_empty = await queue.reviews(since=future)
    assert len(filtered_empty) == 0


async def test_duplicate_question_while_waiting_owner_is_idempotent(queue):
    job = await waiting_job(queue)
    await queue.post_from_executor(job.id, S.MSG_QUESTION, {S.PAYLOAD_TEXT: "ещё раз"})
    assert (await queue.get_job(job.id, CLIENT)).status == S.STATUS_WAITING_OWNER
    kinds = [m.kind for m in await queue.inbox(CLIENT)]
    assert kinds.count(S.MSG_QUESTION) == 2
