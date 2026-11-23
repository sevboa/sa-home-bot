import sys

import pytest

from sa_home_bot.config import DevlabProfileConfig
from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.context import fold_old_results
from sa_home_bot.devlab.agent.notes import AgentNotes
from sa_home_bot.devlab.agent.prompt import CODE_MODE_INTRO_CB_V3_EN, build_system, reminder_for
from sa_home_bot.devlab.agent.prompt_en import CODE_MODE_INTRO_CB_V5_EN
from sa_home_bot.devlab.agent.readstate import STYLE_CB_V5, RepeatLog
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    EXECUTE_DEFINITION,
    EXECUTE_DEFINITION_CB_V5,
    EXECUTE_DEFINITION_EN,
    SCRIPT_OUTPUT_CHARS,
    SCRIPT_OUTPUT_MAX_CHARS,
    TOOL_EXECUTE,
    ToolBox,
    script_output_chars,
)

SOURCE = "".join(f"line {n}\n" for n in range(1, 101))


@pytest.fixture(autouse=True)
def current_python(monkeypatch):
    monkeypatch.setattr(
        tools_module, "worker_argv", lambda config: worker_argv(config, python=sys.executable)
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text(SOURCE)
    (tmp_path / "src" / "pkg" / "b.py").write_text("x = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "n.md").write_text("n\n")
    return tmp_path


def box_for(root, style=STYLE_CB_V5, notes=None, window=0):
    spec = AgentSpec(code_mode=True, prompt_style=style, allow_no_changes=True)
    return ToolBox(LocalSandbox(root), spec, notes=notes, window_tokens=window)


async def run(box, code):
    return (await box.call(TOOL_EXECUTE, {"code": code})).text


def test_prompt_is_english_short_and_free_of_micromanagement():
    assert CODE_MODE_INTRO_CB_V5_EN.startswith("You are an agent working with the user")
    assert "Finishing work" in CODE_MODE_INTRO_CB_V5_EN
    assert len(CODE_MODE_INTRO_CB_V5_EN) < len(CODE_MODE_INTRO_CB_V3_EN) // 2
    for banned in ("150", "6000", "8000", "do not print it whole", "page through"):
        assert banned not in CODE_MODE_INTRO_CB_V5_EN
    assert "[Reminder]" in reminder_for("glm-5.3", True, "ru", STYLE_CB_V5)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_build_system_cb_v5_is_english_whatever_the_lang(lang):
    system = build_system(
        [("r", "text")], model="glm-5.3", code_mode=True, lang=lang, style=STYLE_CB_V5
    )
    assert system.startswith(CODE_MODE_INTRO_CB_V5_EN)
    assert "Project rules (mandatory):" in system


def test_profile_accepts_cb_v5_and_default_is_unchanged():
    assert DevlabProfileConfig(prompt_style="cb_v5").prompt_style == "cb_v5"
    assert DevlabProfileConfig().prompt_style == "cb_v3"


def test_definitions_by_style(project):
    assert box_for(project).definitions[1] is EXECUTE_DEFINITION_CB_V5
    assert "def todo(" in EXECUTE_DEFINITION_CB_V5["function"]["description"]
    assert "def glob(" in EXECUTE_DEFINITION_CB_V5["function"]["description"]
    assert box_for(project, "cb_v3").definitions[1] is EXECUTE_DEFINITION
    for style in ("cb_v1", "cb_v2", "cb_v3", "cb_v4"):
        description = box_for(project, style).definitions[1]["function"]["description"]
        assert "def todo(" not in description and "def glob(" not in description
    spec = AgentSpec(code_mode=True, prompt_style="cb_v3", prompt_lang="en")
    assert ToolBox(LocalSandbox(project), spec).definitions[1] is EXECUTE_DEFINITION_EN


async def test_aliases_read_offset_limit_write_glob(project):
    box = box_for(project)
    try:
        out = await run(box, "print(read('src/a.py', offset=3, limit=2), end='')")
        assert out == "line 3\nline 4"
        assert await run(box, "print(len(read('docs/n.md')))") == "2"
        await run(box, "write('docs/new.md', 'hi')")
        assert (project / "docs" / "new.md").read_text() == "hi\n"
        found = await run(box, "print(glob('src/**/*.py'))")
        assert found == "['src/a.py', 'src/pkg/b.py']"
        assert await run(box, "print(glob('src/*.py'))") == "['src/a.py']"
        numbered = await run(box, "print(read_numbered('src/a.py', 2, 1))")
        assert numbered == "2\tline 2"
    finally:
        await box._sandbox.session_close()


async def test_older_styles_keep_start_end_and_have_no_new_names(project):
    box = box_for(project, "cb_v4")
    try:
        assert await run(box, "print(read('src/a.py', 2, 3), end='')") == "line 2\nline 3"
        assert "NameError" in await run(box, "glob('src/*.py')")
        assert "NameError" in await run(box, "todo(['x'])")
    finally:
        await box._sandbox.session_close()


async def test_todo_is_kept_apart_from_notes_and_survives_new_segment(project, tmp_path):
    notes = AgentNotes(tmp_path / "notes.md")
    box = box_for(project, notes=notes)
    try:
        items = "['first', ('second', 'in_progress'), ('third', 'done')]"
        await run(box, f"note('fact one')\ntodo({items})")
        await run(box, "note('fact two')")
        assert notes.text() == "fact one\nfact two"
        assert notes.todo_text() == "[ ] first\n[~] second\n[x] third"
        assert await run(box, "print(todo())") == "[ ] first\n[~] second\n[x] third"
        await run(box, "notes_set('compact')\ntodo(['only'])")
        assert notes.text() == "compact" and notes.todo_text() == "[ ] only"
    finally:
        await box._sandbox.session_close()
    fresh = box_for(project, notes=AgentNotes(tmp_path / "notes.md"))
    assert fresh.notes_text() == "compact\n\nTodo:\n[ ] only"
    assert box_for(project, "cb_v3", notes=notes).notes_text() == "compact"


def test_todo_only_block_and_limit(tmp_path):
    notes = AgentNotes(tmp_path / "n.md")
    assert notes.set_todo([("a", "pending")]) is None
    assert notes.text() == "" and notes.block_text() == "Todo:\n[ ] a"
    assert notes.set_todo([("x" * 3000, "pending")]) is not None
    assert notes.todo_text() == "[ ] a"


async def test_repeat_mark_for_unchanged_range_and_reset_by_edit(project):
    box = box_for(project)
    try:
        first = await run(box, "x = read('src/a.py', 1, 50)\nprint(len(x))")
        assert "unchanged" not in first
        again = await run(box, "print(read('src/a.py', 10, 20))")
        assert again.startswith("[unchanged since call 1 — the content is above")
        partial = await run(box, "print(len(read('src/a.py', 40, 60)))")
        assert "unchanged" not in partial
        assert "since call 3" in await run(box, "print(read('src/a.py', 1, 60))")
        await run(box, "edit('src/a.py', 'line 5\\n', 'line five\\n')")
        assert "line five" in await run(box, "print(read('src/a.py', 4, 6))")
        assert "unchanged" in await run(box, "print(read_numbered('src/a.py', 4, 6))")
    finally:
        await box._sandbox.session_close()


async def test_repeat_log_resets_on_fold_and_state_loss(project):
    box = box_for(project)
    try:
        await run(box, "read('src/a.py')")
        assert "unchanged" in await run(box, "print(read('src/a.py', 1, 5))")
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "a" * 3000},
        ]
        assert fold_old_results(messages, 10_000, 2, None, box.reset_reads) > 0
        assert "unchanged" not in await run(box, "print(read('src/a.py', 1, 5))")
        fold_old_results(messages, 100, 2, None, box.reset_reads)
        assert "unchanged" not in await run(box, "print(read('src/a.py', 1, 2))")
        assert "unchanged" in await run(box, "print(read('src/a.py', 1, 2))")
        box._repeats.reset()
        assert "unchanged" not in await run(box, "print(read('src/a.py', 1, 2))")
    finally:
        await box._sandbox.session_close()


async def test_clipped_output_is_not_marked_as_seen(project):
    (project / "src" / "big.txt").write_text("y" * 50_000)
    box = box_for(project)
    try:
        clipped = await run(box, "print(read('src/big.txt'))\nprint(read('src/a.py'))")
        assert "обрезано" in clipped
        assert "line 100" in await run(box, "print(read('src/a.py'))")
    finally:
        await box._sandbox.session_close()


async def test_failed_script_is_not_marked_as_seen(project):
    box = box_for(project)
    try:
        await run(box, "x = read('src/a.py')\nraise ValueError('boom')")
        assert "line 100" in await run(box, "print(read('src/a.py'))")
    finally:
        await box._sandbox.session_close()


async def test_mark_not_issued_twice_in_a_row(project):
    box = box_for(project)
    try:
        await run(box, "read('src/a.py')")
        assert "unchanged" in await run(box, "print(read('src/a.py'))")
        assert "line 100" in await run(box, "print(read('src/a.py'))")
        assert "unchanged" in await run(box, "print(read('src/a.py'))")
    finally:
        await box._sandbox.session_close()


def test_forget_call_and_consecutive_unit():
    log = RepeatLog()
    assert log.check("f", "d", 1, 10, 1) == ""
    log.forget_call(1)
    assert log.check("f", "d", 1, 10, 2) == ""
    assert "call 2" in log.check("f", "d", 1, 10, 3)
    assert log.check("f", "d", 1, 10, 4) == ""
    assert "unchanged" in log.check("f", "d", 1, 10, 5)


def test_repeat_log_unit():
    log = RepeatLog()
    assert log.check("f", "d1", 1, 10, 1) == ""
    assert log.check("f", "d1", 11, 20, 2) == ""
    assert "call 2" in log.check("f", "d1", 5, 15, 3)
    assert log.check("f", "d1", 5, 25, 3) == ""
    assert log.check("f", "d2", 5, 15, 4) == ""
    log.reset()
    assert log.check("f", "d2", 5, 15, 5) == ""


def test_output_limit_scales_with_window():
    assert script_output_chars(0, SCRIPT_OUTPUT_CHARS) == SCRIPT_OUTPUT_CHARS
    assert script_output_chars(20_000, SCRIPT_OUTPUT_CHARS) == SCRIPT_OUTPUT_CHARS
    assert script_output_chars(60_000, SCRIPT_OUTPUT_CHARS) == 21_600
    assert script_output_chars(118_000, SCRIPT_OUTPUT_CHARS) == SCRIPT_OUTPUT_MAX_CHARS
    assert script_output_chars(1_000_000, 50_000) == 50_000


async def test_script_output_uses_scaled_limit_only_in_cb_v5(project):
    (project / "big.txt").write_text("y" * 30_000)
    code = "print(read('big.txt'))"
    wide = box_for(project, window=118_000)
    narrow = box_for(project, "cb_v4", window=118_000)
    try:
        assert len(await run(wide, code)) == 30_000
        clipped = await run(narrow, code)
        assert f"показано начало {SCRIPT_OUTPUT_CHARS} симв." in clipped
    finally:
        await wide._sandbox.session_close()
        await narrow._sandbox.session_close()
