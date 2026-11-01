from __future__ import annotations

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.dialog import ANSWER_PREFIX
from sa_home_bot.devlab.agent.loop import AgentLimits, AgentStatus
from sa_home_bot.devlab.agent.model import ModelError
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    TOOL_ASK_OWNER,
    TOOL_FINISH,
    TOOL_RUN_TESTS,
    TOOL_STEP_DONE,
)
from sa_home_bot.devlab.executor.agent_worker import AGENT_DIR
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import ScriptedChatModel, tool_reply
from tests.unit.test_devlab_agent import GREEN_TEST, RED_TEST, edit, make_loop
from tests.unit.test_devlab_agent_worker import (
    FINISH,
    MODEL,
    WRITE_DONE,
    agent_of,
    hub_branch,
    make_worker,
    submit,
    wip_of,
)

env = executor_tests.env
ASK = tool_reply(TOOL_ASK_OWNER, "q1", question="какой формат?")
STEP = tool_reply(TOOL_STEP_DONE, "s1", title="заготовка")
RUN_TESTS = [tool_reply(TOOL_RUN_TESTS, f"t{i}") for i in range(10)]


class Ticker:
    def __init__(self, step: float = 1.0) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


class SpySandbox(LocalSandbox):
    def __init__(self, root, baseline, journal: list[str]) -> None:
        super().__init__(root, baseline)
        self.journal = journal

    async def start(self) -> None:
        self.journal.append("start")

    async def stop(self) -> None:
        self.journal.append("stop")

    async def resume(self) -> None:
        self.journal.append("resume")


def spy_factory(journal: list[str]):
    return lambda request: SpySandbox(request.workdir, request.baseline, journal)


async def test_ask_owner_returns_dialog_and_answer_continues(tmp_path):
    model = ScriptedChatModel(ASK, edit(), tool_reply(TOOL_FINISH, "f", summary="ок"))
    loop, _ = make_loop(tmp_path, model)

    asked = await loop.run("s", "t")

    assert asked.status is AgentStatus.ASKED
    assert asked.dialog.question == "какой формат?"
    assert asked.dialog.call_id == "q1"
    resumed = await loop.run("s", "t", resume=asked.dialog.model_copy(update={"answer": "json"}))
    assert resumed.status is AgentStatus.FINISHED
    last = model.seen[1][-1]
    assert last == {"role": "tool", "tool_call_id": "q1", "content": ANSWER_PREFIX + "json"}


async def test_ask_owner_skips_sibling_calls_in_same_reply(tmp_path):
    reply = ASK
    sibling = tool_reply(TOOL_RUN_TESTS, "t9").tool_calls[0]
    reply = type(reply)("", (*reply.tool_calls, sibling), tokens_in=1, tokens_out=1)
    loop, _ = make_loop(tmp_path, ScriptedChatModel(reply))

    asked = await loop.run("s", "t")

    assert [m["tool_call_id"] for m in asked.dialog.messages if m["role"] == "tool"] == ["t9"]


async def test_step_done_rejected_on_red_and_accepted_on_green(tmp_path):
    steps: list[str] = []

    async def on_step(title: str) -> None:
        steps.append(title)

    for test_argv, accepted in ((RED_TEST, False), (GREEN_TEST, True)):
        steps.clear()
        spec = AgentSpec(test_argv=test_argv, diff_prefixes=("src/",))
        model = ScriptedChatModel(STEP, ModelError("конец"))
        loop, _ = make_loop(tmp_path, model, spec=spec)
        loop._on_step = on_step
        await loop.run("s", "t")
        answer = model.seen[1][-1]["content"]
        assert (steps == ["заготовка"]) is accepted
        assert answer.startswith("шаг принят" if accepted else "шаг не принят")


@pytest.mark.parametrize(
    ("limits", "spec_tests", "extra", "reason"),
    [
        (AgentLimits(max_steps=3), GREEN_TEST, {}, "лимит шагов"),
        (AgentLimits(max_red_runs=2), RED_TEST, {}, "красных прогонов"),
        (AgentLimits(max_wall_s=3), GREEN_TEST, {"clock": Ticker()}, "бюджет времени"),
        (AgentLimits(max_cost_usd=0.5), GREEN_TEST, {"spend": Ticker(0.2)}, "потолок расхода"),
        (AgentLimits(max_tokens=250), GREEN_TEST, {}, "потолок токенов"),
    ],
)
async def test_stop_cranes_ask_owner_with_reason(tmp_path, limits, spec_tests, extra, reason):
    spec = AgentSpec(test_argv=spec_tests, diff_prefixes=("src/",))
    model = ScriptedChatModel(*RUN_TESTS)
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=limits)
    loop._clock = extra.get("clock", loop._clock)
    loop._spend = extra.get("spend")

    result = await loop.run("s", "t")

    assert result.status is AgentStatus.ASKED
    assert reason in result.reason
    assert result.dialog.call_id is None
    assert result.dialog.question.startswith("Стоп-кран")
    assert len(model.script) > 5


async def test_green_run_resets_red_streak_and_answer_grants_new_segment(tmp_path):
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",))
    model = ScriptedChatModel(*RUN_TESTS[:2], tool_reply(TOOL_FINISH, "f", summary="x"), edit())
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=AgentLimits(max_steps=2))
    first = await loop.run("s", "t")
    assert first.status is AgentStatus.ASKED

    answered = first.dialog.model_copy(update={"answer": "продолжай"})
    second = await loop.run("s", "t", resume=answered)

    assert second.status is AgentStatus.ASKED
    assert model.seen[2][-1] == {"role": "user", "content": ANSWER_PREFIX + "продолжай"}


async def parked_worker(env, script, **kwargs):
    journal: list[str] = []
    model = ScriptedChatModel(*script)
    worker = make_worker(env, model, sandboxes=spy_factory(journal), **kwargs)
    return worker, model, journal


async def test_question_answer_continues_same_dialog(env):
    job_id = await submit(env)
    worker, model, journal = await parked_worker(env, [WRITE_DONE, STEP, ASK, FINISH])

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_WAITING_OWNER
    assert journal == ["start", "stop"]
    assert worker.parked_jobs == [job_id]
    assert await hub_branch(env, wip_of(job_id))
    question = await env.store.queue.last_message(job_id, protocol.MSG_QUESTION)
    assert question.payload[protocol.PAYLOAD_TEXT] == "какой формат?"
    assert not await worker.claim_once()

    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="json")
    await worker.poll_once()
    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert journal == ["start", "stop", "resume"]
    assert model.seen[-1][-1]["content"] == ANSWER_PREFIX + "json"
    assert len(model.seen[-1]) == len(model.seen[-2]) + 2
    kinds = await env.inbox_kinds()
    assert kinds == [
        protocol.MSG_STARTED,
        protocol.MSG_STEP_DONE,
        protocol.MSG_QUESTION,
        protocol.MSG_DONE,
    ]
    assert await hub_branch(env, agent_of(job_id))
    assert await hub_branch(env, wip_of(job_id)) is None
    assert not worker.parked_jobs
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()


async def test_restart_between_question_and_answer(env):
    job_id = await submit(env)
    first, _, _ = await parked_worker(env, [WRITE_DONE, ASK])
    assert await first.claim_once()

    journal: list[str] = []
    model = ScriptedChatModel(FINISH)
    restarted = make_worker(env, model, sandboxes=spy_factory(journal))
    assert restarted.parked_jobs == [job_id]
    assert not await restarted.claim_once()

    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="yaml")
    await restarted.poll_once()
    assert await restarted.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert journal == ["start"]
    assert model.seen[0][-1]["content"] == ANSWER_PREFIX + "yaml"
    assert await env.inbox_kinds() == [
        protocol.MSG_STARTED,
        protocol.MSG_QUESTION,
        protocol.MSG_DONE,
    ]


async def test_restart_after_answer_taken_still_has_answer(env):
    job_id = await submit(env)
    first, _, _ = await parked_worker(env, [WRITE_DONE, ASK])
    assert await first.claim_once()
    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="toml")
    await first.poll_once()

    model = ScriptedChatModel(FINISH)
    restarted = make_worker(env, model)
    assert await restarted.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert model.seen[0][-1]["content"] == ANSWER_PREFIX + "toml"


async def test_resume_with_other_model_starts_fresh_with_journal_and_remarks(env):
    job_id = await submit(env)
    worker, _, _ = await parked_worker(env, [WRITE_DONE, STEP, ASK], models=(MODEL, "glm"))
    assert await worker.claim_once()

    await env.mcp.call(protocol.TOOL_RESUME, id=job_id, text="учти кодировку", model="glm")
    await worker.poll_once()
    assert not worker.parked_jobs
    model = ScriptedChatModel(FINISH)
    names: list[str] = []
    worker._models = lambda name, jid: names.append(name) or model

    assert await worker.claim_once()

    assert names == ["glm"]
    task = model.seen[0][1]["content"]
    assert "Журнал шагов" in task and "1. заготовка" in task
    assert "учти кодировку" in task
    assert "out.txt" in task
    assert ANSWER_PREFIX not in str(model.seen[0])
    assert await env.status(job_id) == protocol.STATUS_DONE


async def test_stop_while_waiting_owner_keeps_partial_branch(env):
    job_id = await submit(env)
    worker, _, _ = await parked_worker(env, [WRITE_DONE, ASK])
    assert await worker.claim_once()

    await env.mcp.call(protocol.TOOL_STOP, id=job_id)
    await worker.poll_once()

    assert await env.status(job_id) == protocol.STATUS_STOPPED
    assert not worker.parked_jobs
    assert await hub_branch(env, wip_of(job_id)) is None
    assert await hub_branch(env, agent_of(job_id))
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()
    assert not worker.pending_messages


async def test_step_limit_crane_becomes_question_not_failure(env):
    job_id = await submit(env)
    worker, _, _ = await parked_worker(env, [WRITE_DONE], limits=AgentLimits(max_steps=1))

    assert await worker.claim_once()

    assert await env.status(job_id) == protocol.STATUS_WAITING_OWNER
    question = await env.store.queue.last_message(job_id, protocol.MSG_QUESTION)
    assert "лимит шагов" in question.payload[protocol.PAYLOAD_TEXT]
