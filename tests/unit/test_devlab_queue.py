from __future__ import annotations

from datetime import timedelta

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.store import HubStore
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
        await queue.submit(CLIENT, PROJECT, MODEL, TASK, kind=S.JOB_KIND_TEST)
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


async def test_release_only_after_finish(queue):
    job = await running_job(queue)
    with pytest.raises(HubError):
        await queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE)
    await queue.post_from_executor(job.id, S.MSG_DONE)
    await queue.post_from_client(CLIENT, job.id, S.MSG_RELEASE)
    assert (await queue.get_job(job.id)).status == S.STATUS_DONE


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
