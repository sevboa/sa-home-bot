import json
import os
import sys

import pytest

from sa_home_bot.devlab.agent.context import (
    DROPPED_STEPS_PATTERN,
    fold_old_results,
    set_notes_block,
    task_text,
)
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import AgentLoop, AgentStatus
from sa_home_bot.devlab.agent.notes import (
    NOTES_CLOSE,
    NOTES_OPEN,
    AgentNotes,
    notes_block,
    prune_notes,
)
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import TOOL_FINISH, ToolBox
from tests.unit.support.fakes import ScriptedChatModel, tool_reply
from tests.unit.test_devlab_agent_context import dialog
from tests.unit.test_devlab_agent_script import Session


def box_with(tmp_path, notes):
    return ToolBox(LocalSandbox(tmp_path), AgentSpec(code_mode=True), notes=notes)


def notes_at(tmp_path, limit=100):
    return AgentNotes(tmp_path / "notes" / "job.md", limit)


async def run_in_script(tmp_path, box, code):
    session = Session(tmp_path)
    try:
        return await session.run(code, on_call=lambda request: box._serve_call(request, None, []))
    finally:
        await session.close()


def test_notes_accumulate_and_survive_new_instance(tmp_path):
    first = notes_at(tmp_path)
    assert first.text() == ""
    assert first.add("сигнатура load(path)") is None
    assert first.add("  путь src/a.py  ") is None
    assert notes_at(tmp_path).text() == "сигнатура load(path)\nпуть src/a.py"


def test_note_over_limit_is_refused_and_keeps_text(tmp_path):
    notes = notes_at(tmp_path, limit=30)
    assert notes.add("a" * 20) is None
    refusal = notes.add("b" * 20)
    assert refusal is not None and "notes_set" in refusal
    assert notes.text() == "a" * 20
    assert notes.add("   ") is not None


def test_replace_compresses_and_checks_limit(tmp_path):
    notes = notes_at(tmp_path, limit=30)
    notes.add("a" * 25)
    assert notes.replace("b" * 31) is not None
    assert notes.text() == "a" * 25
    assert notes.replace("кратко") is None
    assert notes.text() == "кратко"
    assert notes.add("ещё") is None
    notes.discard()
    assert notes.text() == ""


def test_prune_removes_only_old_files(tmp_path):
    old, fresh = tmp_path / "old.md", tmp_path / "fresh.md"
    old.write_text("x")
    fresh.write_text("y")
    os.utime(old, (1000, 1000))
    os.utime(fresh, (9000, 9000))
    assert prune_notes(tmp_path, 3600, now=9500) == 1
    assert not old.exists() and fresh.exists()
    assert prune_notes(tmp_path / "нет", 1) == 0


async def test_helpers_write_read_replace_and_report_full(tmp_path):
    notes = notes_at(tmp_path, limit=40)
    box = box_with(tmp_path, notes)
    run = await run_in_script(tmp_path, box, "note('первая')\nnote('вторая')\nprint(notes())")
    assert run.output == "первая\nвторая"
    full = await run_in_script(tmp_path, box, "note('x' * 40)\nprint('дальше')")
    assert "notes_set" in full.output and full.output.endswith("дальше")
    assert notes.text() == "первая\nвторая"
    await run_in_script(tmp_path, box, "notes_set('итог')")
    assert notes.text() == "итог"
    kept = await run_in_script(tmp_path, box, "notes()")
    assert kept.output == "итог"


async def test_helpers_without_notes_file_say_unavailable(tmp_path):
    run = await run_in_script(tmp_path, box_with(tmp_path, None), "note('a')")
    assert "заметки недоступны" in run.output


async def test_helper_rejects_non_string(tmp_path):
    run = await run_in_script(tmp_path, box_with(tmp_path, notes_at(tmp_path)), "note(5)")
    assert run.failed and "TypeError" in run.output


def messages_with(task="задание"):
    return [{"role": "system", "content": "s"}, {"role": "user", "content": task}]


def test_block_set_replaced_and_removed():
    messages = messages_with()
    assert set_notes_block(messages, "факт 1")
    assert messages[1]["content"] == "задание" + notes_block("факт 1")
    assert not set_notes_block(messages, "факт 1")
    assert set_notes_block(messages, "факт 2")
    assert messages[1]["content"].count(NOTES_OPEN) == 1
    assert "факт 1" not in messages[1]["content"]
    assert set_notes_block(messages, "")
    assert messages[1]["content"] == "задание"
    assert not set_notes_block(messages, "")


def test_block_sits_before_dropped_marker_and_task_text_hides_both():
    messages = dialog(8, output=2000)
    set_notes_block(messages, "важное")
    fold_old_results(messages, 700, 1)
    set_notes_block(messages, "важное 2")
    content = messages[1]["content"]
    assert DROPPED_STEPS_PATTERN.search(content)
    assert content.index(NOTES_CLOSE) < content.index("[свёрнуто")
    assert task_text(messages) == "t"


def test_fold_refreshes_block_in_task_and_leaves_it_unfolded():
    messages = dialog(8, output=6000)
    current = {"text": "старое"}
    fold_old_results(messages, 13000, 2, lambda: current["text"])
    assert "старое" in messages[1]["content"]
    current["text"] = "новое"
    messages += dialog(8, output=6000)[2:]
    fold_old_results(messages, 13000, 2, lambda: current["text"])
    assert "новое" in messages[1]["content"]
    assert "старое" not in messages[1]["content"]


def test_fold_without_need_does_not_touch_block():
    messages = dialog(1, output=10)
    assert fold_old_results(messages, 13000, 2, lambda: "заметка") > 0
    assert messages[1]["content"] == "t"


def test_empty_notes_add_no_block_on_fold():
    messages = dialog(8, output=6000)
    fold_old_results(messages, 13000, 2, lambda: "")
    assert NOTES_OPEN not in messages[1]["content"]


def loop_with(tmp_path, notes, model):
    spec = AgentSpec(
        test_argv=(sys.executable, "-c", "pass"), diff_prefixes=("src/",), allow_no_changes=True
    )
    sandbox = LocalSandbox(tmp_path)
    return AgentLoop(model, ToolBox(sandbox, spec, notes=notes), FinishGuard(sandbox, spec))


async def test_block_is_in_first_request_of_segment(tmp_path):
    notes = notes_at(tmp_path, 500)
    notes.add("load живёт в src/a.py")
    model = ScriptedChatModel(tool_reply(TOOL_FINISH, summary="готово"))
    result = await loop_with(tmp_path, notes, model).run("s", "задача")
    assert result.status is AgentStatus.FINISHED
    assert model.seen[0][1]["content"] == "задача" + notes_block("load живёт в src/a.py")


async def test_no_block_when_notes_empty(tmp_path):
    model = ScriptedChatModel(tool_reply(TOOL_FINISH, summary="готово"))
    await loop_with(tmp_path, notes_at(tmp_path), model).run("s", "задача")
    assert model.seen[0][1]["content"] == "задача"


async def test_resumed_segment_gets_current_notes_not_stale_ones(tmp_path):
    notes = notes_at(tmp_path, 500)
    notes.add("актуально")
    stale = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "задача" + notes_block("устарело")},
    ]
    resumed = Dialog(messages=stale, question="q", answer="ответ")
    model = ScriptedChatModel(tool_reply(TOOL_FINISH, summary="готово"))
    await loop_with(tmp_path, notes, model).run("s", "задача", resume=resumed)
    first = model.seen[0][1]["content"]
    assert "актуально" in first and "устарело" not in first


@pytest.mark.parametrize("text", ["", "заметка"])
def test_task_text_ignores_block(text):
    messages = messages_with("задание")
    set_notes_block(messages, text)
    assert task_text(messages) == "задание"
    json.dumps(messages)
