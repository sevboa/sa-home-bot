import sys

import pytest

from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.loop import AgentStatus
from sa_home_bot.devlab.agent.model import ModelError
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import PROCESS_FORBIDDEN, SCRIPT_FAILED_HEAD, script_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    SCRIPT_ONLY_TEXT,
    TOOL_ASK_OWNER,
    TOOL_EXECUTE,
    TOOL_FINISH,
    TOOL_READ_FILE,
    TOOL_SEARCH,
    TOOL_STEP_DONE,
    ToolBox,
    ToolError,
)
from tests.unit.support.fakes import ScriptedChatModel, tool_reply
from tests.unit.test_devlab_agent import GREEN_TEST, make_loop

GREEN_CHECK = (sys.executable, "-c", "import sys; sys.exit(0)")
RED_CHECK = (sys.executable, "-c", "import sys; sys.exit(1)")
EDIT_APP = "edit('src/app.py', 'VALUE = 1', 'VALUE = 2')\n"
PYTEST_ARGV = (sys.executable, "-m", "pytest", "-q")


@pytest.fixture(autouse=True)
def current_python(monkeypatch):
    monkeypatch.setattr(
        tools_module,
        "script_argv",
        lambda code, config: script_argv(code, config, python=sys.executable),
    )


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("value = 1\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "n.md").write_text("старое\n")
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    return tmp_path


def toolbox(root, **spec):
    return ToolBox(LocalSandbox(root), AgentSpec(code_mode=True, **spec))


async def test_without_code_mode_new_tools_are_absent(project):
    box = ToolBox(LocalSandbox(project), AgentSpec())
    names = [d["function"]["name"] for d in box.definitions]
    assert TOOL_EXECUTE not in names
    assert TOOL_SEARCH not in names
    with pytest.raises(ToolError):
        await box.call(TOOL_EXECUTE, {"code": "print(1)"})


async def test_code_mode_offers_only_search_and_execute(project):
    box = toolbox(project)
    assert [d["function"]["name"] for d in box.definitions] == [TOOL_SEARCH, TOOL_EXECUTE]
    for name in (TOOL_READ_FILE, TOOL_STEP_DONE, TOOL_FINISH, TOOL_ASK_OWNER):
        with pytest.raises(ToolError, match=SCRIPT_ONLY_TEXT):
            await box.call(name, {})


@pytest.mark.parametrize(
    ("query", "present", "absent"),
    [("finish", "def finish(", "def grep("), ("", "def grep(", None), ("нет-такого", "нет", None)],
)
async def test_search_returns_stubs(project, query, present, absent):
    outcome = await toolbox(project).call(TOOL_SEARCH, {"query": query})
    assert present in outcome.text
    if absent:
        assert absent not in outcome.text


async def test_edit_is_visible_to_next_read_and_applied_at_once(project):
    box = toolbox(project)
    code = "edit('src/a.py', 'value = 1', 'value = 2')\nprint(read('src/a.py'))"
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    assert outcome.text == "value = 2"
    assert (project / "src" / "a.py").read_text() == "value = 2\n"
    assert outcome.mutating


async def test_edit_error_and_role_paths(project):
    box = toolbox(project, write_prefixes=("docs/",))
    code = (
        "for args in [('src/a.py', 'value = 1', 'x'), ('docs/n.md', 'нет', 'x')]:\n"
        "    try:\n        edit(*args)\n    except EditError as e:\n        print(e)\n"
        "write('docs/n.md', 'новое\\n')"
    )
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    assert "src/a.py вне путей роли" in outcome.text
    assert "найден 0 раз" in outcome.text
    assert (project / "src" / "a.py").read_text() == "value = 1\n"
    assert (project / "docs" / "n.md").read_text() == "новое\n"


async def test_open_write_outside_role_paths_is_reverted(project):
    box = toolbox(project, write_prefixes=("docs/",))
    code = "open('src/a.py', 'w').write('x = 0\\n')\nopen('docs/n.md', 'w').write('ok\\n')"
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    assert "src/a.py вне путей роли" in outcome.text
    assert (project / "src" / "a.py").read_text() == "value = 1\n"
    assert (project / "docs" / "n.md").read_text() == "ok\n"
    assert outcome.mutating


async def test_long_output_keeps_head_and_failure_keeps_tail(project):
    box = toolbox(project, output_chars=100)
    outcome = await box.call(TOOL_EXECUTE, {"code": "print('начало' + 'x' * 30000)"})
    assert outcome.text.startswith("начало")
    assert "обрезано: показано начало" in outcome.text
    failed = await box.call(
        TOOL_EXECUTE, {"code": "print('x' * 30000)\nraise ValueError('конец')"}
    )
    assert failed.text.startswith(SCRIPT_FAILED_HEAD)
    assert "ValueError: конец" in failed.text
    assert "exec(compile" not in failed.text


@pytest.mark.parametrize(
    ("runs", "green"),
    [(["test_bad.py", "test_ok.py"], True), (["test_ok.py", "test_bad.py"], False), ([], None)],
)
async def test_green_comes_from_last_run_tests(project, runs, green):
    box = toolbox(project, test_argv=PYTEST_ARGV)
    code = "\n".join(f"run_tests([{path!r}])" for path in runs) or "print('нет')"
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    assert outcome.green is green


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nsubprocess.run(['true'])",
        "import os\nos.system('true')",
        "import os\nos.execv('/bin/true', ['true'])",
    ],
)
async def test_processes_from_script_are_forbidden(project, code):
    outcome = await toolbox(project).call(TOOL_EXECUTE, {"code": code})
    assert outcome.text.startswith(SCRIPT_FAILED_HEAD)
    assert PROCESS_FORBIDDEN in outcome.text


async def test_project_packages_import_from_src(project):
    (project / "src" / "pkg").mkdir()
    (project / "src" / "pkg" / "__init__.py").write_text("NAME = 'пакет'\n")
    outcome = await toolbox(project).call(TOOL_EXECUTE, {"code": "import pkg\nprint(pkg.NAME)"})
    assert outcome.text.splitlines()[0] == "пакет"


async def execute_loop(tmp_path, code, checks=()):
    steps = []

    async def on_step(title):
        steps.append(title)

    spec = AgentSpec(test_argv=GREEN_TEST, checks=checks, diff_prefixes=("src/",), code_mode=True)
    model = ScriptedChatModel(tool_reply(TOOL_EXECUTE, "s1", code=code), ModelError("конец"))
    loop, _ = make_loop(tmp_path, model, spec=spec)
    loop._on_step = on_step
    result = await loop.run("s", "t")
    answer = model.seen[1][-1]["content"] if len(model.seen) > 1 else ""
    return result, answer, steps


@pytest.mark.parametrize(("check", "accepted"), [(GREEN_CHECK, True), (RED_CHECK, False)])
async def test_step_done_from_script_goes_through_the_guard(tmp_path, check, accepted):
    code = EDIT_APP + 'step_done("итог шага")'
    result, answer, steps = await execute_loop(tmp_path, code, checks=(check,))
    assert result.status is AgentStatus.MODEL_FAILED
    assert (steps == ["итог шага"]) is accepted
    assert ("step_done: шаг принят" in answer) is accepted
    assert ("step_done: шаг не принят" in answer) is not accepted


@pytest.mark.parametrize(
    ("edits", "finished"), [(EDIT_APP, True), ("print('нет правок')\n", False)]
)
async def test_finish_from_script_goes_through_the_guard(tmp_path, edits, finished):
    result, answer, _ = await execute_loop(tmp_path, edits + 'finish("всё готово")')
    assert (result.status is AgentStatus.FINISHED) is finished
    if finished:
        assert result.summary == "всё готово"
    else:
        assert "финал отклонён" in answer


async def test_branch_on_test_result_finishes_or_asks(tmp_path):
    code = (
        EDIT_APP
        + "r = run_tests(['test_nowhere.py'])\n"
        + "if r.ok:\n    finish('готово')\nelse:\n    ask_owner('тесты красные, что делать?')"
    )
    spec = AgentSpec(
        test_argv=(sys.executable, "-m", "pytest", "-q"), diff_prefixes=("src/",), code_mode=True
    )
    model = ScriptedChatModel(tool_reply(TOOL_EXECUTE, "s1", code=code))
    loop, _ = make_loop(tmp_path, model, spec=spec)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.ASKED
    assert result.reason == "тесты красные, что делать?"
    assert result.dialog.call_id == "s1"
