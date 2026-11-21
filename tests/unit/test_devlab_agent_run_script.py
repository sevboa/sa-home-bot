import sys

import pytest

from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import PROCESS_FORBIDDEN, SCRIPT_FAILED_HEAD, script_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    SCRIPT_ONLY_TEXT,
    TOOL_ASK_OWNER,
    TOOL_FINISH,
    TOOL_READ_FILE,
    TOOL_RUN_SCRIPT,
    TOOL_STEP_DONE,
    ToolBox,
    ToolError,
)

PYTEST_ARGV = (sys.executable, "-m", "pytest", "-q")


@pytest.fixture(autouse=True)
def current_python(monkeypatch):
    monkeypatch.setattr(
        tools_module, "script_argv", lambda code: script_argv(code, python=sys.executable)
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


async def test_without_code_mode_run_script_is_absent(project):
    box = ToolBox(LocalSandbox(project), AgentSpec())
    assert TOOL_RUN_SCRIPT not in [d["function"]["name"] for d in box.definitions]
    with pytest.raises(ToolError):
        await box.call(TOOL_RUN_SCRIPT, {"code": "print(1)"})


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("print('\\n'.join(read('src/a.py')))", "1\tvalue = 1"),
        ("print(len(list(read('src/a.py'))))", "1"),
        ("print(read('src/a.py') == '\\n'.join(read('src/a.py')))", "True"),
    ],
)
async def test_read_iterates_lines_not_characters(project, code, expected):
    outcome = await toolbox(project).call(TOOL_RUN_SCRIPT, {"code": code})
    assert expected in outcome.text


async def test_edit_in_script_applies_after_script(project):
    box = toolbox(project)
    assert TOOL_RUN_SCRIPT in [d["function"]["name"] for d in box.definitions]
    code = "print(read('src/a.py'))\nedit('src/a.py', 'value = 1', 'value = 2')"
    outcome = await box.call(TOOL_RUN_SCRIPT, {"code": code})
    assert (project / "src" / "a.py").read_text() == "value = 2\n"
    assert "1\tvalue = 1" in outcome.text
    assert "действие 1 edit_file: правка src/a.py" in outcome.text
    assert outcome.mutating


async def test_action_outside_role_paths_stops_the_rest(project):
    box = toolbox(project, write_prefixes=("docs/",))
    code = "edit('src/a.py', 'value = 1', 'value = 2')\nwrite('docs/n.md', 'новое\\n')"
    outcome = await box.call(TOOL_RUN_SCRIPT, {"code": code})
    assert "действие 1 edit_file: ошибка: путь src/a.py вне путей роли" in outcome.text
    assert "действие 2 write_file: пропущено после ошибки" in outcome.text
    assert (project / "src" / "a.py").read_text() == "value = 1\n"
    assert (project / "docs" / "n.md").read_text() == "старое\n"
    assert not outcome.mutating


async def test_long_output_keeps_head_and_failure_keeps_tail(project):
    box = toolbox(project, output_chars=100)
    outcome = await box.call(TOOL_RUN_SCRIPT, {"code": "print('начало' + 'x' * 30000)"})
    assert outcome.text.startswith("начало")
    assert "обрезано: показано начало" in outcome.text
    failed = await box.call(
        TOOL_RUN_SCRIPT, {"code": "print('x' * 30000)\nraise ValueError('конец')"}
    )
    assert failed.text.startswith(SCRIPT_FAILED_HEAD)
    assert "ValueError: конец" in failed.text
    assert "exec(compile" not in failed.text


async def test_direct_write_from_script_blocks_actions(project):
    box = toolbox(project)
    code = "open('src/a.py', 'w').write('x = 0\\n')\nwrite('docs/n.md', 'новое\\n')"
    outcome = await box.call(TOOL_RUN_SCRIPT, {"code": code})
    assert "src/a.py" in outcome.text
    assert (project / "docs" / "n.md").read_text() == "старое\n"
    assert outcome.mutating


@pytest.mark.parametrize(
    ("runs", "green"),
    [(["test_bad.py", "test_ok.py"], True), (["test_ok.py", "test_bad.py"], False), ([], None)],
)
async def test_green_comes_from_last_run_tests(project, runs, green):
    box = toolbox(project, test_argv=PYTEST_ARGV)
    code = "\n".join(f"run_tests([{path!r}])" for path in runs) or "print('нет')"
    outcome = await box.call(TOOL_RUN_SCRIPT, {"code": code})
    assert outcome.green is green


async def test_code_mode_hides_plain_tools(project):
    box = toolbox(project)
    names = [d["function"]["name"] for d in box.definitions]
    assert names == [TOOL_RUN_SCRIPT, TOOL_STEP_DONE, TOOL_ASK_OWNER, TOOL_FINISH]
    with pytest.raises(ToolError, match=SCRIPT_ONLY_TEXT):
        await box.call(TOOL_READ_FILE, {"path": "src/a.py"})


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess\nsubprocess.run(['true'])",
        "import os\nos.system('true')",
        "import os\nos.execv('/bin/true', ['true'])",
    ],
)
async def test_processes_from_script_are_forbidden(project, code):
    outcome = await toolbox(project).call(TOOL_RUN_SCRIPT, {"code": code})
    assert outcome.text.startswith(SCRIPT_FAILED_HEAD)
    assert PROCESS_FORBIDDEN in outcome.text


async def test_project_packages_import_from_src(project):
    (project / "src" / "pkg").mkdir()
    (project / "src" / "pkg" / "__init__.py").write_text("NAME = 'пакет'\n")
    code = "import pkg\nprint(pkg.NAME)"
    outcome = await toolbox(project).call(TOOL_RUN_SCRIPT, {"code": code})
    assert outcome.text.splitlines()[0] == "пакет"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"{sys.executable} -c 'print(5)'", "действие 1 run_command: [код 0]"),
        ("ls", "действие 1 run_command: ошибка: команда не разрешена"),
    ],
)
async def test_run_action_goes_through_allowed_commands(project, command, expected):
    box = toolbox(project, allowed_commands=(f"{sys.executable} -c",))
    outcome = await box.call(TOOL_RUN_SCRIPT, {"code": f"run({command!r})"})
    assert expected in outcome.text
