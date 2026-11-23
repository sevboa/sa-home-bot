import sys

import pytest

from sa_home_bot.config import DevlabProfileConfig
from sa_home_bot.devlab.agent import markup as markup_module
from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import AgentLoop
from sa_home_bot.devlab.agent.markup import (
    IDLE_CALLS_LIMIT,
    STYLE_CB_V4,
    Markup,
    ReadLog,
    build_toc,
    mentioned_paths,
    render_plan,
    task_steps,
)
from sa_home_bot.devlab.agent.notes import AgentNotes
from sa_home_bot.devlab.agent.prompt import (
    CODE_MODE_INTRO_CB_V1,
    CODE_MODE_INTRO_CB_V2,
    CODE_MODE_INTRO_CB_V3,
    CODE_MODE_INTRO_CB_V4,
    CODE_MODE_INTRO_REMOTE,
    build_system,
)
from sa_home_bot.devlab.agent.prompt_en import (
    CODE_MODE_INTRO_CB_V1_EN,
    CODE_MODE_INTRO_CB_V2_EN,
    CODE_MODE_INTRO_CB_V3_EN,
    CODE_MODE_INTRO_CB_V4_EN,
)
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import TOOL_EXECUTE, TOOL_FINISH, ToolBox
from tests.unit.support.fakes import ScriptedChatModel, tool_reply

PY_SOURCE = """LIMIT = 3
NAME: str = "x"


class Host(Base):
    def run(self, a: int, *, b=2) -> str:
        return "x"

    async def stop(self):
        pass


def load(path, strict=False) -> dict:
    return {}
"""
TASK = """Допиши `src/a.py`, ориентир `data/m.json`, каталог `pkg/`, нет `src/ghost.py`.

## Шаги
1. Тест на новое поведение
2. Правка кода
3. Запись в docs
"""


@pytest.fixture(autouse=True)
def current_python(monkeypatch):
    monkeypatch.setattr(
        tools_module, "worker_argv", lambda config: worker_argv(config, python=sys.executable)
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(PY_SOURCE)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "m.json").write_text(
        '{"metrics": [1, 2, 3], "meta": {"a": 1, "b": 2}, "name": "n", "ok": true}'
    )
    (tmp_path / "pkg" / "deep" / "deeper").mkdir(parents=True)
    (tmp_path / "pkg" / "one.txt").write_text("1\n")
    (tmp_path / "pkg" / "deep" / "two.txt").write_text("22\n")
    (tmp_path / "pkg" / "deep" / "deeper" / "three.txt").write_text("333\n")
    (tmp_path / "notes.md").write_text("a\nb\n")
    return tmp_path


def box_for(root, style=STYLE_CB_V4, notes=None):
    spec = AgentSpec(code_mode=True, prompt_style=style, allow_no_changes=True)
    return ToolBox(LocalSandbox(root), spec, notes=notes)


async def run(box, code):
    return (await box.call(TOOL_EXECUTE, {"code": code})).text


async def test_toc_describes_python_json_dir_other_and_missing(project):
    toc = await build_toc(LocalSandbox(project), TASK + " `notes.md` `src/a.py:10`")
    assert "src/a.py (14 стр.," in toc
    assert "L1-2 LIMIT, NAME" in toc
    assert "L5-10 class Host(Base)" in toc
    assert "    L6-7 def run(self, a: int, *, b=2) -> str" in toc
    assert "    L9-10 async def stop(self)" in toc
    assert "L13-14 def load(path, strict=False) -> dict" in toc
    assert "data/m.json (1 стр.," in toc
    assert "metrics: список[3]" in toc
    assert "meta: {a, b}" in toc
    assert "name: строка" in toc and "ok: булево" in toc
    assert "pkg/ (3 файлов)" in toc
    assert "one.txt" in toc and "deep/two.txt" in toc
    assert "deeper/three.txt" not in toc and "ещё 1 файлов глубже" in toc
    assert "src/ghost.py — не найден" in toc
    assert "notes.md (2 стр.," in toc
    assert toc.count("src/a.py") == 1


async def test_toc_falls_back_on_broken_python_and_json(tmp_path):
    (tmp_path / "bad.py").write_text("def (:\n")
    (tmp_path / "bad.json").write_text("{")
    toc = await build_toc(LocalSandbox(tmp_path), "`bad.py` `bad.json`")
    assert "bad.py (1 стр." in toc and "bad.json (1 стр." in toc
    assert "L1" not in toc


async def test_toc_respects_limits(project, monkeypatch):
    monkeypatch.setattr(markup_module, "TOC_MAX_PATHS", 1)
    toc = await build_toc(LocalSandbox(project), TASK)
    assert "src/a.py" in toc and "data/m.json" not in toc
    assert "ещё 3 путей не показаны" in toc
    monkeypatch.setattr(markup_module, "TOC_MAX_PATHS", 12)
    monkeypatch.setattr(markup_module, "TOC_SECTION_CHARS", 120)
    short = await build_toc(LocalSandbox(project), "`src/a.py`")
    assert "… ещё" in short and len(short) < 200
    monkeypatch.setattr(markup_module, "TOC_SECTION_CHARS", 1400)
    monkeypatch.setattr(markup_module, "TOC_LIMIT_CHARS", 200)
    capped = await build_toc(LocalSandbox(project), TASK)
    assert "оглавление упёрлось в лимит" in capped


async def test_toc_is_empty_without_paths(project):
    assert await build_toc(LocalSandbox(project), "просто текст `finish()`") == ""


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("`a/b.py` и `x.json` и `a/b.py`", ["a/b.py", "x.json"]),
        ("`finish()` `self.x` `os.path` `../etc/x.py` `/abs/x.py`", []),
        ("`tests/unit/` `src/pkg/mod` `mod.py:12-30`", ["tests/unit/", "src/pkg/mod", "mod.py"]),
    ],
)
def test_mentioned_paths(task, expected):
    assert mentioned_paths(task) == expected


def test_task_steps_and_plan_marks():
    steps = task_steps(TASK)
    assert [n for n, _ in steps] == [1, 2, 3]
    assert render_plan(steps, {2}).splitlines() == [
        "[ ] 1. Тест на новое поведение",
        "[x] 2. Правка кода",
        "[ ] 3. Запись в docs",
    ]


def test_read_log_merges_overlaps_and_adjacent_ranges():
    log = ReadLog()
    assert log.record("a.py", "d", 1, 10, 100, 1) is None
    assert log.record("a.py", "d", 8, 20, 100, 2) is None
    assert log.record("a.py", "d", 21, 30, 100, 3) is None
    assert log.record("a.py", "d", 50, 60, 100, 4) is None
    assert log.render() == "a.py 1–30, 50–60 (вызов …, 2, 3, 4)"


def test_read_log_whole_file_and_repeat_hint():
    log = ReadLog()
    assert log.record("a.py", "d", 1, 40, 40, 1) is None
    assert log.render() == "a.py целиком (вызов 1)"
    hint = log.record("a.py", "d", 5, 9, 40, 2)
    assert hint is not None
    assert "уже читал a.py 5–9 на вызове 1" in hint and "note()" in hint
    assert log.render() == "a.py целиком (вызов 1)"


def test_read_log_changed_digest_forgets_file_and_orders_by_recency():
    log = ReadLog()
    log.record("a.py", "d1", 1, 5, 5, 1)
    log.record("b.py", "d", 1, 5, 5, 2)
    assert log.record("a.py", "d2", 1, 5, 5, 3) is None
    assert log.render().splitlines() == ["b.py целиком (вызов 2)", "a.py целиком (вызов 3)"]


def test_read_log_render_is_capped(monkeypatch):
    monkeypatch.setattr(markup_module, "READS_LIMIT_CHARS", 60)
    log = ReadLog()
    for index in range(6):
        log.record(f"file{index}.py", "d", 1, 5, 5, index)
    rendered = log.render()
    assert "file5.py" in rendered and "file0.py" not in rendered and "… ещё" in rendered


def test_stop_reading_fires_every_k_calls_and_resets_on_write():
    state = Markup()
    hints = [state.after_call(False) for _ in range(IDLE_CALLS_LIMIT * 2)]
    assert [i + 1 for i, hint in enumerate(hints) if hint] == [
        IDLE_CALLS_LIMIT,
        IDLE_CALLS_LIMIT * 2,
    ]
    assert "хватит читать" in hints[IDLE_CALLS_LIMIT - 1]
    assert state.after_call(True) == ""
    assert not any(state.after_call(False) for _ in range(IDLE_CALLS_LIMIT - 1))
    assert state.after_call(False)


async def test_block_has_sections_and_user_notes_stay_apart(project, tmp_path):
    state = Markup()
    await state.open_segment(LocalSandbox(project), TASK)
    state.close_steps({1})
    state.record_read("src/a.py", "d", 1, 5, 14)
    block = state.block("мой факт")
    titles = [line for line in block.splitlines() if line.startswith("## ")]
    assert titles == [
        "## Оглавление файлов задачи",
        "## План",
        "## Прочитано",
        "## Заметки",
    ]
    assert "[x] 1." in block and "src/a.py 1–5" in block
    assert Markup().block("") == ""
    assert Markup().block("только заметки") == "## Заметки\nтолько заметки"


async def test_notes_text_merges_markup_without_touching_notes_file(project, tmp_path):
    notes = AgentNotes(tmp_path / "n" / "job.md", 50)
    notes.add("факт")
    box = box_for(project, notes=notes)
    await box.open_segment(TASK)
    text = box.notes_text()
    assert "src/a.py" in text and text.endswith("факт")
    await run(box, "notes_set('новое')")
    assert notes.text() == "новое"
    assert "src/a.py" in box.notes_text()


async def test_read_twice_in_scripts_prints_hint_and_records_journal(project):
    box = box_for(project)
    try:
        first = await run(box, "x = read('src/a.py', 1, 6)\nprint(len(x))")
        assert "уже читал" not in first
        again = await run(box, "read('src/a.py', 2, 4)\nread_numbered('src/a.py', 1, 3)")
        assert again.count("уже читал src/a.py") == 2 and "на вызове 1" in again
        batch = "d = read_many(['src/a.py', 'notes.md'])\nprint(len(d))"
        assert "уже читал" not in await run(box, batch)
        many = await run(box, batch)
        assert many.count("уже читал") == 2 and "src/a.py 1–14 на вызове 1, 3" in many
        journal = box.notes_text()
        assert "src/a.py целиком (вызов 1, 3)" in journal
        assert "notes.md целиком (вызов 3)" in journal
    finally:
        await box._sandbox.session_close()


async def test_edit_makes_next_read_fresh(project):
    box = box_for(project)
    try:
        await run(box, "read('src/a.py')")
        await run(box, "edit('src/a.py', 'LIMIT = 3', 'LIMIT = 4')")
        assert "уже читал" not in await run(box, "read('src/a.py')")
    finally:
        await box._sandbox.session_close()


async def test_stop_reading_hint_in_execute_output_and_reset_by_write(project):
    box = box_for(project)
    try:
        outputs = [await run(box, f"print({n})") for n in range(IDLE_CALLS_LIMIT)]
        assert "хватит читать" not in "".join(outputs[:-1])
        closing = f"{IDLE_CALLS_LIMIT - 1}\n[{IDLE_CALLS_LIMIT} вызовов без записи"
        assert outputs[-1].startswith(closing)
        await run(box, "write('src/new.py', 'x = 1')")
        outputs = [await run(box, "print(0)") for _ in range(IDLE_CALLS_LIMIT)]
        assert "хватит читать" not in "".join(outputs[:-1])
        assert "хватит читать" in outputs[-1]
        assert "хватит читать" in await run(box, "print(1)") + "".join(
            [await run(box, "print(1)") for _ in range(IDLE_CALLS_LIMIT - 1)]
        )
    finally:
        await box._sandbox.session_close()


async def test_hint_is_not_added_to_finishing_call(project):
    box = box_for(project)
    try:
        for _ in range(IDLE_CALLS_LIMIT - 1):
            await run(box, "print(0)")
        outcome = await box.call(TOOL_EXECUTE, {"code": "finish('готово')"})
        assert outcome.control is not None and "хватит читать" not in outcome.text
    finally:
        await box._sandbox.session_close()


@pytest.mark.parametrize("style", ["cb_v1", "cb_v2", "cb_v3"])
async def test_older_styles_have_no_markup(project, style):
    box = box_for(project, style=style)
    try:
        await box.open_segment(TASK)
        await run(box, "read('src/a.py')")
        assert "уже читал" not in await run(box, "read('src/a.py')")
        assert box.notes_text() == ""
        outputs = [await run(box, "print(0)") for _ in range(IDLE_CALLS_LIMIT + 1)]
        assert "хватит читать" not in "".join(outputs)
    finally:
        await box._sandbox.session_close()


async def test_loop_puts_markup_in_first_request_and_marks_closed_steps(project):
    spec = AgentSpec(
        code_mode=True,
        prompt_style=STYLE_CB_V4,
        test_argv=(sys.executable, "-c", "pass"),
        diff_prefixes=("src/",),
        allow_no_changes=True,
    )
    sandbox = LocalSandbox(project)
    box = ToolBox(sandbox, spec)
    model = ScriptedChatModel(
        tool_reply(TOOL_EXECUTE, code="print(1)"), tool_reply(TOOL_FINISH, summary="готово")
    )
    loop = AgentLoop(model, box, FinishGuard(sandbox, spec))
    try:
        await loop.run("s", TASK)
    finally:
        await sandbox.session_close()
    first = model.seen[0][1]["content"]
    assert first.startswith(TASK) and "## Оглавление файлов задачи" in first
    assert "[ ] 1. Тест на новое поведение" in first


def test_prompts_carry_notes_rule_only_in_cb_v4():
    for older in (
        CODE_MODE_INTRO_CB_V1,
        CODE_MODE_INTRO_CB_V2,
        CODE_MODE_INTRO_CB_V3,
        CODE_MODE_INTRO_REMOTE,
        CODE_MODE_INTRO_CB_V1_EN,
        CODE_MODE_INTRO_CB_V2_EN,
        CODE_MODE_INTRO_CB_V3_EN,
    ):
        assert "note(" not in older and "notes_set" not in older
    for newer in (CODE_MODE_INTRO_CB_V4, CODE_MODE_INTRO_CB_V4_EN):
        assert "note(" in newer and "read(" in newer
    assert CODE_MODE_INTRO_CB_V4.startswith(CODE_MODE_INTRO_CB_V3[:500])


@pytest.mark.parametrize(
    ("lang", "marker"), [("ru", "Оглавление, План"), ("en", "Table of contents")]
)
def test_build_system_selects_cb_v4(lang, marker):
    kwargs = {"model": "glm-5.3", "code_mode": True, "lang": lang}
    assert marker in build_system([], style=STYLE_CB_V4, **kwargs)
    assert marker not in build_system([], style="cb_v3", **kwargs)


def test_profile_accepts_cb_v4():
    assert DevlabProfileConfig(prompt_style="cb_v4").prompt_style == "cb_v4"
