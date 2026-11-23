import dataclasses
import sys

import pytest

from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.loop import CODE_COUNTER, AgentStatus
from sa_home_bot.devlab.agent.model import ModelError
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import PROCESS_FORBIDDEN, SCRIPT_FAILED_HEAD, worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    DIFF_EMPTY,
    SCRIPT_ONLY_TEXT,
    SCRIPT_STATE_RESET,
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
        "worker_argv",
        lambda config: worker_argv(config, python=sys.executable),
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


async def execute_loop(tmp_path, code, checks=(), task="t"):
    steps = []

    async def on_step(title):
        steps.append(title)

    spec = AgentSpec(test_argv=GREEN_TEST, checks=checks, diff_prefixes=("src/",), code_mode=True)
    model = ScriptedChatModel(tool_reply(TOOL_EXECUTE, "s1", code=code), ModelError("конец"))
    loop, _ = make_loop(tmp_path, model, spec=spec)
    loop._on_step = on_step
    result = await loop.run("s", task)
    answer = model.seen[1][-1]["content"] if len(model.seen) > 1 else ""
    return result, answer, steps


@pytest.mark.parametrize(("check", "accepted"), [(GREEN_CHECK, True), (RED_CHECK, False)])
async def test_step_done_from_script_goes_through_the_guard(tmp_path, check, accepted):
    code = EDIT_APP + 'step_done("итог шага")\nprint("после шага")'
    result, answer, steps = await execute_loop(tmp_path, code, checks=(check,))
    assert result.status is AgentStatus.MODEL_FAILED
    assert (steps == ["итог шага"]) is accepted
    assert ("после шага" in answer) is accepted
    assert ("StepRejected: шаг не принят" in answer) is not accepted
    assert answer.startswith(SCRIPT_FAILED_HEAD) is not accepted


async def test_accepted_step_done_prints_no_noise_and_counts_the_call(tmp_path):
    result, answer, steps = await execute_loop(tmp_path, EDIT_APP + 'step_done("итог шага")')
    assert steps == ["итог шага"]
    assert "ничего не напечатал" not in answer
    assert answer == CODE_COUNTER.format(number=1, thousands=0)


async def test_silent_script_without_step_or_signal_keeps_hint(tmp_path):
    _, answer, _ = await execute_loop(tmp_path, "x = 1")
    assert "ничего не напечатал" in answer


async def test_finish_after_accepted_step_done_in_one_script(tmp_path):
    code = EDIT_APP + 'step_done("итог шага")\nfinish("всё готово")'
    result, _, steps = await execute_loop(tmp_path, code)
    assert result.status is AgentStatus.FINISHED
    assert result.summary == "всё готово"
    assert steps == ["итог шага"]


STEPS_TASK = (
    "# Задача\n\n## Цель\n1. не шаг\n\n## Шаги\n1. тест.\n2. правка.\n3. docs.\n\n"
    "## Тесты\n1. команда\n"
)


async def test_finish_closes_unclosed_steps_of_the_task(tmp_path):
    code = EDIT_APP + 'step_done("1–2: тест и правка")\nfinish("всё готово")'
    result, _, steps = await execute_loop(tmp_path, code, task=STEPS_TASK)
    assert result.status is AgentStatus.FINISHED
    assert steps == ["1–2: тест и правка", "3: закрыт на finish: всё готово"]


async def test_finish_without_step_done_closes_every_step(tmp_path):
    result, _, steps = await execute_loop(
        tmp_path, EDIT_APP + 'finish("итог")', task=STEPS_TASK
    )
    assert result.status is AgentStatus.FINISHED
    assert [s.split(":")[0] for s in steps] == ["1", "2", "3"]


async def test_finish_without_parsed_steps_closes_nothing(tmp_path):
    _, _, steps = await execute_loop(tmp_path, EDIT_APP + 'finish("итог")', task="просто текст")
    assert steps == []


async def test_rejected_finish_closes_no_steps(tmp_path):
    _, _, steps = await execute_loop(
        tmp_path, EDIT_APP + 'finish("итог")', checks=(RED_CHECK,), task=STEPS_TASK
    )
    assert steps == []


async def test_rejected_step_done_counts_red_and_next_call_can_retry(tmp_path):
    spec = AgentSpec(
        test_argv=GREEN_TEST, checks=(RED_CHECK,), diff_prefixes=("src/",), code_mode=True
    )
    model = ScriptedChatModel(
        tool_reply(TOOL_EXECUTE, "s1", code=EDIT_APP + 'step_done("шаг")'),
        tool_reply(TOOL_EXECUTE, "s2", code='step_done("шаг")'),
        ModelError("конец"),
    )
    loop, _ = make_loop(tmp_path, model, spec=spec)
    await loop.run("s", "t")
    first, second = model.seen[1][-1]["content"], model.seen[2][-1]["content"]
    assert "StepRejected: шаг не принят" in first
    assert "StepRejected: шаг не принят" in second
    assert second.endswith(CODE_COUNTER.format(number=2, thousands=0))


async def test_counter_shows_call_number_and_context_in_thousands(tmp_path):
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",), code_mode=True)
    big = dataclasses.replace(tool_reply(TOOL_EXECUTE, "s1", code="print(1)"), tokens_in=25400)
    model = ScriptedChatModel(big, tool_reply(TOOL_EXECUTE, "s2", code="print(2)"), ModelError("x"))
    loop, _ = make_loop(tmp_path, model, spec=spec)
    await loop.run("s", "t")
    assert model.seen[1][-1]["content"] == "1\n[вызов 1 · контекст 25 тыс. токенов]"
    assert model.seen[2][-1]["content"] == "2\n[вызов 2 · контекст 0 тыс. токенов]"


@pytest.mark.parametrize(
    ("through", "first_failed"),
    [("finish", "test_bad.py::test_bad"), ("step_done", "test_new_bad.py::test_new")],
)
async def test_guard_rejection_lists_failed_tests_before_tail(tmp_path, through, first_failed):
    spec = AgentSpec(test_argv=PYTEST_ARGV, diff_prefixes=("src/",), code_mode=True)
    files = {
        "src/app.py": "VALUE = 1\n",
        "test_bad.py": "def test_bad():\n    assert False\n",
        "test_ok.py": "def test_ok():\n    assert True\n",
    }
    call = 'finish("готово")' if through == "finish" else 'step_done("шаг")'
    new_test = "write('test_new_bad.py', 'def test_new():\\n    assert False\\n')\n"
    model = ScriptedChatModel(
        tool_reply(TOOL_EXECUTE, "s1", code=EDIT_APP + new_test + call), ModelError("конец")
    )
    loop, _ = make_loop(tmp_path, model, spec=spec, files=files)
    await loop.run("s", "t")
    answer = model.seen[1][-1]["content"]
    assert f"упавшие тесты:\nFAILED {first_failed}" in answer
    assert answer.index("упавшие тесты:") < answer.index("хвост вывода:")


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


async def test_variables_persist_between_execute_calls(project):
    box = toolbox(project)
    await box.call(TOOL_EXECUTE, {"code": "src = read('src/a.py')"})
    outcome = await box.call(TOOL_EXECUTE, {"code": "edit('src/a.py', src, 'value = 2\\n')\nsrc"})
    await box._sandbox.session_close()
    assert outcome.text == "value = 1"
    assert (project / "src" / "a.py").read_text() == "value = 2\n"


async def test_state_survives_finish_signal_in_execute(project):
    box = toolbox(project)
    await box.call(TOOL_EXECUTE, {"code": "kept = 5"})
    signalled = await box.call(TOOL_EXECUTE, {"code": "finish('готово')"})
    after = await box.call(TOOL_EXECUTE, {"code": "kept"})
    await box._sandbox.session_close()
    assert signalled.control is not None
    assert after.text == "5"


async def test_timeout_resets_state_and_says_so(project, monkeypatch):
    monkeypatch.setattr(AgentSpec, "script_timeout_s", property(lambda self: 1.0))
    box = toolbox(project)
    await box.call(TOOL_EXECUTE, {"code": "kept = 5"})
    timed_out = await box.call(TOOL_EXECUTE, {"code": "import time\ntime.sleep(30)"})
    after = await box.call(TOOL_EXECUTE, {"code": "print('kept' in globals())"})
    await box._sandbox.session_close()
    assert timed_out.text.startswith("[таймаут 1 с]")
    assert SCRIPT_STATE_RESET in timed_out.text
    assert after.text == "False"


async def test_process_death_reports_state_reset(project):
    box = toolbox(project)
    died = await box.call(TOOL_EXECUTE, {"code": "import os\nos._exit(3)"})
    await box._sandbox.session_close()
    assert died.text.startswith(SCRIPT_FAILED_HEAD)
    assert "код 3" in died.text
    assert SCRIPT_STATE_RESET in died.text


@pytest.fixture
def ruled_project(project):
    (project / "tests" / "unit").mkdir(parents=True)
    (project / "tests" / "unit" / "test_no_comments_in_code.py").write_text("")
    return project


async def test_run_tests_flags_comments_in_changed_files_against_base(ruled_project):
    box = toolbox(ruled_project, test_argv=PYTEST_ARGV)
    code = (
        "open('src/a.py', 'w').write('value = 2  # поясняю\\n')\n"
        "write('src/clean.py', 'x = 1\\n')\n"
        "open('tests/unit/test_x.py', 'w').write("
        "'def test_x():\\n    '+chr(34)*3+'док'+chr(34)*3+'\\n')\n"
        "r = run_tests(['test_ok.py'])\n"
        "print(r.ok)\nprint(r.output)"
    )
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    await box._sandbox.session_close()
    lines = outcome.text.splitlines()
    assert lines[0] == "False"
    assert "комментарии запрещены: src/a.py:1 # поясняю" in lines
    assert not any("src/clean.py" in line for line in lines)
    assert any(line.startswith("комментарии запрещены: tests/unit/test_x.py:2") for line in lines)
    assert outcome.green is False


async def test_run_tests_ignores_comments_when_project_has_no_such_rule(project):
    box = toolbox(project, test_argv=PYTEST_ARGV)
    code = "edit('src/a.py', 'value = 1', 'value = 2  # ок')\nprint(run_tests(['test_ok.py']).ok)"
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    await box._sandbox.session_close()
    assert outcome.text == "True"


async def test_diff_shows_changes_against_base_tree(project, tmp_path_factory):
    base = tmp_path_factory.mktemp("base")
    (base / "src").mkdir()
    (base / "src" / "a.py").write_text("value = 1\n")
    sandbox = LocalSandbox(project, LocalSandbox(project).snapshot(), base)
    box = ToolBox(sandbox, AgentSpec(code_mode=True))
    code = "edit('src/a.py', 'value = 1', 'value = 3')\nprint(diff())"
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    await sandbox.session_close()
    assert "--- a/src/a.py\n+++ b/src/a.py" in outcome.text
    assert "-value = 1\n+value = 3" in outcome.text


async def test_diff_of_clean_tree_says_so(project):
    box = toolbox(project)
    outcome = await box.call(TOOL_EXECUTE, {"code": "print(diff())"})
    await box._sandbox.session_close()
    assert outcome.text == DIFF_EMPTY


async def test_diff_includes_docs_despite_diff_prefixes(project, tmp_path_factory):
    base = tmp_path_factory.mktemp("base")
    for name, text in (("src/a.py", "value = 1\n"), ("docs/n.md", "старое\n")):
        (base / name).parent.mkdir(exist_ok=True)
        (base / name).write_text(text)
    sandbox = LocalSandbox(project, LocalSandbox(project).snapshot(), base)
    box = ToolBox(sandbox, AgentSpec(code_mode=True, diff_prefixes=("src/",)))
    code = "write('docs/n.md', 'новое\\n')\nprint(diff())"
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    await sandbox.session_close()
    assert "--- a/docs/n.md\n+++ b/docs/n.md" in outcome.text


async def test_english_lang_offers_english_definitions_and_search(project):
    box = toolbox(project, prompt_lang="en")
    execute = box.definitions[1]["function"]["description"]
    assert execute.startswith("Runs Python code")
    assert "def finish(summary: str) -> NoReturn" in execute
    assert "завершить" not in execute
    outcome = await box.call(TOOL_SEARCH, {"query": "finish"})
    assert "finish the task" in outcome.text


async def test_edit_rejects_new_comment_but_ignores_old_ones(ruled_project):
    box = toolbox(ruled_project, test_argv=PYTEST_ARGV)
    code = (
        "try:\n"
        "    edit('src/a.py', 'value = 1', 'value = 2  # поясняю')\n"
        "except EditError as error:\n"
        "    print(error)\n"
        "print(read('src/a.py'))"
    )
    outcome = await box.call(TOOL_EXECUTE, {"code": code})
    await box._sandbox.session_close()
    assert "комментарии запрещены: src/a.py:1 # поясняю" in outcome.text
    assert "value = 1" in outcome.text



async def test_assigning_api_name_is_restored_with_note(project):
    box = toolbox(project)
    first = await box.call(TOOL_EXECUTE, {"code": "read = 5\ncount = 3"})
    assert "assigning read shadowed the API function read; it is restored" in first.text
    second = await box.call(TOOL_EXECUTE, {"code": "print(read('src/a.py'), count)"})
    assert second.text.endswith(" 3")
