from __future__ import annotations

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.dialog import ANSWER_PREFIX, Dialog
from sa_home_bot.devlab.agent.loop import AgentLoop, AgentResult, AgentStatus, Crane
from sa_home_bot.devlab.executor import agent_worker as agent_worker_module
from sa_home_bot.devlab.executor.agent_worker import (
    AGENT_DIR,
    AGENT_STATE_FILE,
    DIALOG_FILE,
    AgentState,
)
from sa_home_bot.utils.files import load_model
from tests.unit import test_devlab_executor as executor_tests
from tests.unit.support.fakes import ScriptedChatModel
from tests.unit.test_devlab_agent_owner import spy_factory
from tests.unit.test_devlab_agent_repo import hub_branch
from tests.unit.test_devlab_agent_worker import (
    FINISH,
    WRITE_DONE,
    make_worker,
    submit,
    wip_of,
)

env = executor_tests.env
PROJECT = executor_tests.PROJECT

PARK_MESSAGES = [
    {"role": "system", "content": "правила"},
    {"role": "user", "content": "# сделай out.txt"},
    {"role": "assistant", "content": ""},
    {"role": "tool", "tool_call_id": "w1", "content": "готово"},
]
PARKED_DIALOG = Dialog(messages=PARK_MESSAGES, question="остановить?", parked=True)
QUESTION_DIALOG = Dialog(
    messages=[{"role": "system", "content": "правила"}, {"role": "user", "content": "задача"}],
    question="стоп-кран?",
)


class ParkLoop(AgentLoop):
    resumes: list[Dialog] = []

    def __init__(self, model, tools, guard, limits, **kwargs) -> None:
        super().__init__(model, tools, guard, limits, **kwargs)

    async def run(self, system: str, task: str, resume: Dialog | None = None) -> AgentResult:
        if resume is None:
            await self._model.complete(PARKED_DIALOG.messages, [])
            return AgentResult(AgentStatus.ASKED, dialog=PARKED_DIALOG, crane=Crane.PARK)
        ParkLoop.resumes.append(resume)
        await self._model.complete(resume.messages, [])
        return AgentResult(AgentStatus.FINISHED, summary="сделано")


class QuestionLoop(AgentLoop):
    def __init__(self, model, tools, guard, limits, **kwargs) -> None:
        super().__init__(model, tools, guard, limits, **kwargs)

    async def run(self, system: str, task: str, resume: Dialog | None = None) -> AgentResult:
        if resume is None:
            await self._model.complete(QUESTION_DIALOG.messages, [])
            return AgentResult(AgentStatus.ASKED, dialog=QUESTION_DIALOG, crane=Crane.COST)
        return AgentResult(AgentStatus.FINISHED, summary="сделано")


async def park_job(env, model, monkeypatch, loop_class):
    monkeypatch.setattr(agent_worker_module, "AgentLoop", loop_class)
    journal: list[str] = []
    worker = make_worker(env, model, sandboxes=spy_factory(journal))
    job_id = await submit(env)
    assert await worker.claim_once()
    assert worker.parked_jobs == [job_id]
    return worker, model, journal, job_id


async def test_parked_crane_keeps_job_with_executor_and_hub_silent(env, monkeypatch):
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker, model, journal, job_id = await park_job(env, model, monkeypatch, ParkLoop)

    assert await env.status(job_id) == protocol.STATUS_RUNNING
    assert await env.inbox_kinds() == [protocol.MSG_STARTED]
    root = env.tmp_path / "exec" / AGENT_DIR / job_id
    assert (root / DIALOG_FILE).exists()
    dialog = load_model(Dialog, root / DIALOG_FILE)
    assert dialog.parked
    assert dialog.question == "остановить?"
    assert dialog.answer is None
    state = load_model(AgentState, env.tmp_path / "exec" / AGENT_STATE_FILE)
    assert state.job is None
    assert state.spend[job_id].tokens == 110
    assert state.spend[job_id].extensions == 0
    assert not worker._busy.busy()
    assert worker._busy.running_jobs() == []
    assert worker._busy.holding_for_owner() == []
    assert journal == ["start", "stop"]
    assert await hub_branch(env, wip_of(job_id))
    assert (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()


async def test_restart_resumes_parked_before_new_claims(env, monkeypatch):
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    first, _, journal, job_id = await park_job(env, model, monkeypatch, ParkLoop)
    ParkLoop.resumes = []
    other_id = await submit(env, push=False)

    restarted_model = ScriptedChatModel(FINISH)
    restarted = make_worker(env, restarted_model)
    assert restarted.parked_jobs == [job_id]
    assert not restarted._busy.busy()
    assert restarted._busy.holding_for_owner() == []

    assert await restarted.claim_once()

    assert len(ParkLoop.resumes) == 1
    resume = ParkLoop.resumes[-1]
    assert resume.parked
    assert resume.answer is None
    assert resume.messages == PARK_MESSAGES
    assert not any(str(m["content"]).startswith(ANSWER_PREFIX) for m in resume.messages)
    assert restarted_model.seen[0][-1]["role"] == "tool"
    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.status(other_id) == protocol.STATUS_QUEUED
    done = await env.store.queue.last_message(job_id, protocol.MSG_DONE)
    assert done.payload[protocol.PAYLOAD_TOKENS] == 220
    assert done.payload[protocol.PAYLOAD_COST_USD] == 0.0
    assert await env.inbox_kinds() == [
        protocol.MSG_STARTED,
        protocol.MSG_STARTED,
        protocol.MSG_DONE,
    ]
    assert await hub_branch(env, wip_of(job_id)) is None
    assert not restarted.parked_jobs
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()


async def test_owner_question_job_waits_for_answer_as_before(env, monkeypatch):
    model = ScriptedChatModel(WRITE_DONE, FINISH)
    worker, model, journal, job_id = await park_job(env, model, monkeypatch, QuestionLoop)

    assert await env.status(job_id) == protocol.STATUS_WAITING_OWNER
    question = await env.store.queue.last_message(job_id, protocol.MSG_QUESTION)
    assert question.payload[protocol.PAYLOAD_TEXT] == "стоп-кран?"
    assert worker._busy.holding_for_owner() == [job_id]
    assert worker.parked_jobs == [job_id]

    restarted = make_worker(env, ScriptedChatModel(FINISH))
    assert not await restarted.claim_once()
    assert restarted._busy.holding_for_owner() == [job_id]

    await env.mcp.call(protocol.TOOL_ANSWER, id=job_id, text="продолжай")
    await restarted.poll_once()
    assert await restarted.claim_once()

    assert await env.status(job_id) == protocol.STATUS_DONE
    assert await env.inbox_kinds() == [
        protocol.MSG_STARTED,
        protocol.MSG_QUESTION,
        protocol.MSG_STARTED,
        protocol.MSG_DONE,
    ]
    assert not restarted._busy.busy()
    assert not restarted.parked_jobs
    assert not (env.tmp_path / "exec" / AGENT_DIR / job_id).exists()
