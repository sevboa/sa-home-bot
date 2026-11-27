from __future__ import annotations

import pytest

from sa_home_bot.devlab.agent.dialog import ANSWER_PREFIX, Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import AgentLoop, AgentStatus, Crane
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import TOOL_FINISH, ToolBox
from tests.unit.support.fakes import ScriptedChatModel, tool_reply
from tests.unit.test_devlab_agent import GREEN_TEST, edit


@pytest.fixture
def sandbox(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n")
    return LocalSandbox(tmp_path)


def park_loop(sandbox, model, park):
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",))
    return AgentLoop(model, ToolBox(sandbox, spec), FinishGuard(sandbox, spec), park=park)


def park_dialog(dialog):
    return Dialog(messages=dialog.messages, question=dialog.question, parked=True)


def test_parked_dialog_resumes_as_is_without_owner_answer():
    messages = [
        {"role": "system", "content": "правила"},
        {"role": "user", "content": "задача"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "tool_call_id": "c1", "content": "готово"},
    ]
    dialog = Dialog(messages=messages, question="остановить?", parked=True)
    resumed = dialog.resumed_messages()
    assert resumed == messages
    assert resumed[-1]["role"] == "tool"
    assert not any(str(m["content"]).startswith(ANSWER_PREFIX) for m in resumed)


async def test_park_before_first_model_call_parks_with_initial_dialog(tmp_path, sandbox):
    model = ScriptedChatModel(edit())
    loop = park_loop(sandbox, model, lambda: True)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.ASKED
    assert result.crane is Crane.PARK
    assert model.seen == []
    assert [m["role"] for m in result.dialog.messages] == ["system", "user"]
    assert result.dialog.question
    assert result.dialog.parked
    assert (tmp_path / "src/app.py").read_text() == "VALUE = 1\n"


async def test_park_after_started_step_finishes_it_and_parks(tmp_path, sandbox):
    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, "f", summary="готово"))
    loop = park_loop(sandbox, model, lambda: bool(model.seen))
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.ASKED
    assert result.crane is Crane.PARK
    assert len(model.seen) == 1
    assert (tmp_path / "src/app.py").read_text() == "VALUE = 2\n"
    messages = result.dialog.messages
    assert messages[-2]["role"] == "assistant"
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["tool_call_id"] == "c1"
    assert result.dialog.question
    assert result.dialog.parked


async def test_park_false_runs_to_finish_as_before(tmp_path, sandbox):
    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, "f", summary="готово"))
    loop = park_loop(sandbox, model, lambda: False)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED
    assert result.summary == "готово"


async def test_parked_resume_continues_without_new_owner_reply(tmp_path, sandbox):
    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, "f", summary="готово"))
    loop = park_loop(sandbox, model, lambda: bool(model.seen))
    parked = await loop.run("правила", "задача")
    assert parked.crane is Crane.PARK
    follow = ScriptedChatModel(tool_reply(TOOL_FINISH, "f", summary="готово"))
    loop2 = park_loop(sandbox, follow, lambda: False)
    snapshot = list(parked.dialog.messages)
    second = await loop2.run("правила", "задача", resume=park_dialog(parked.dialog))
    assert second.status is AgentStatus.FINISHED
    assert second.summary == "готово"
    assert follow.seen[0] == snapshot
    assert follow.seen[0][-1]["role"] == "tool"
    assert not any(str(m["content"]).startswith(ANSWER_PREFIX) for m in follow.seen[0])


async def test_parked_resume_then_park_again_parks_at_next_boundary(tmp_path, sandbox):
    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, "f", summary="готово"), edit())
    loop = park_loop(sandbox, model, lambda: bool(model.seen))
    parked = await loop.run("правила", "задача")
    assert parked.crane is Crane.PARK
    loop2 = park_loop(sandbox, model, lambda: bool(model.seen))
    again = await loop2.run("правила", "задача", resume=park_dialog(parked.dialog))
    assert again.status is AgentStatus.ASKED
    assert again.crane is Crane.PARK
    assert again.dialog.question
    assert again.dialog.parked
    assert [m["role"] for m in again.dialog.messages][-2:] == ["assistant", "tool"]


async def test_plain_dialog_without_answer_still_fails(tmp_path, sandbox):
    plain = Dialog(messages=[{"role": "user", "content": "задача"}], question="стоп?")
    with pytest.raises(ValueError):
        plain.resumed_messages()
    follow = ScriptedChatModel(tool_reply(TOOL_FINISH, "f", summary="готово"))
    loop = park_loop(sandbox, follow, lambda: False)
    with pytest.raises(ValueError):
        await loop.run("правила", "задача", resume=plain)
    assert follow.seen == []
