from __future__ import annotations

import asyncio
import sqlite3
from datetime import timedelta

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.queue import SCHEMA as S_QUEUE_SCHEMA
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


async def submit(queue, model=MODEL, priority=0, client=CLIENT):
    return await queue.submit(client, PROJECT, model, TASK, priority)


async def running_job(queue):
    job = await submit(queue)
    await queue.claim(EXECUTOR, [MODEL])
    return job


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
    if target in S.TRANSITIONS[status] or confirms_stop:
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


async def test_claim_order_priority_then_age(queue):
    low = await submit(queue)
    high = await submit(queue, priority=5)
    low2 = await submit(queue)
    order = [(await queue.claim(EXECUTOR, [MODEL])).id for _ in range(3)]
    assert order == [high.id, low.id, low2.id]
    assert await queue.claim(EXECUTOR, [MODEL]) is None


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
