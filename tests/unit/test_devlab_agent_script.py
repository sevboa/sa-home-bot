import sys

import pytest

from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import SCRIPT_API, api_stubs, parse_output, script_argv

CONFIG = {
    "closed": [],
    "write_prefixes": [],
    "allowed": [[sys.executable, "-c"]],
    "test_argv": [sys.executable, "-m", "pytest", "-q"],
    "command_timeout": 30,
    "test_timeout": 60,
    "budget": 100,
    "output_chars": 6000,
}


async def run_script(root, code, **config):
    argv = script_argv(code, {**CONFIG, **config}, python=sys.executable)
    result = await LocalSandbox(root).exec(argv, 120)
    return parse_output(result.output)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha = 1\nbeta = 2\ngamma = 3\n")
    (tmp_path / "src" / "b.py").write_text("beta = 20\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("beta\n")
    return tmp_path


async def test_read_many_and_print_reach_output(project):
    code = "texts = read_many(['src/a.py', 'src/b.py'])\nprint(texts['src/b.py'])\nprint(ls())"
    run = await run_script(project, code)
    assert not run.failed
    assert run.control is None
    assert "beta = 20" in run.output
    assert "1\t" not in run.output
    assert "['src/a.py', 'src/b.py']" in run.output


async def test_read_is_plain_text_and_numbered_has_numbers(project):
    run = await run_script(
        project, "print(repr(read('src/a.py', 2, 3)))\nprint(read_numbered('src/a.py', 2, 3))"
    )
    assert run.output.splitlines() == ["'beta = 2\\ngamma = 3\\n'", "2\tbeta = 2", "3\tgamma = 3"]


async def test_grep_with_context_marks_match_and_neighbours(project):
    run = await run_script(project, "print('\\n'.join(grep('^beta', 'src/a', context=1)))")
    assert run.output.splitlines() == [
        "src/a.py-1-alpha = 1",
        "src/a.py:2:beta = 2",
        "src/a.py-3-gamma = 3",
    ]


async def test_exception_marks_script_failed_with_traceback(project):
    run = await run_script(project, "edit('src/a.py', 'alpha', 'b')\nraise LookupError('нет')")
    assert run.failed
    assert "LookupError: нет" in run.output
    assert (project / "src" / "a.py").read_text().startswith("b = 1")


@pytest.mark.parametrize("text", ["просто вывод\n", "DEVLAB_ACTIONS {битый"])
def test_output_without_valid_marker_is_failed(text):
    run = parse_output(text)
    assert run.failed
    assert run.control is None
    assert run.output == text.rstrip()


@pytest.mark.parametrize(("query", "count"), [("", len(SCRIPT_API)), ("тест", 1), ("grep", 1)])
def test_search_lists_matching_stubs(query, count):
    assert len(api_stubs(query).splitlines()) == count
    assert "find" not in api_stubs()


@pytest.mark.parametrize(
    ("code", "tool", "args"),
    [
        ("step_done('шаг')", "step_done", {"title": "шаг"}),
        ("finish('итог')", "finish", {"summary": "итог"}),
        ("ask_owner('вопрос?')", "ask_owner", {"question": "вопрос?"}),
    ],
)
async def test_control_calls_end_the_script_even_inside_try(project, code, tool, args):
    run = await run_script(
        project,
        f"print('до')\ntry:\n    {code}\nexcept Exception:\n    print('поймано')\nprint('после')",
    )
    assert not run.failed
    assert run.output == "до"
    assert (run.control.tool, run.control.args) == (tool, args)


async def test_edit_is_immediate_and_edit_error_names_reason(project):
    code = (
        "edit('src/a.py', 'alpha = 1', 'alpha = 5')\n"
        "print(read('src/a.py', 1, 1).strip())\n"
        "try:\n    edit('src/a.py', 'нет такого', 'x')\nexcept EditError as e:\n    print(e)"
    )
    run = await run_script(project, code)
    assert run.output.splitlines()[0] == "alpha = 5"
    assert "найден 0 раз" in run.output


async def test_write_through_open_is_accepted_when_path_allowed(project):
    code = "open('src/c.py', 'w').write('x = 1\\n')"
    run = await run_script(project, code, write_prefixes=["src/"])
    assert not run.failed
    assert (project / "src" / "c.py").read_text() == "x = 1\n"


@pytest.mark.parametrize(
    ("code", "kwargs", "reason"),
    [
        ("open('docs.md', 'w').write('x')", {"write_prefixes": ["src/"]}, "вне путей роли"),
        ("open('src/a.py', 'w').write('x')", {"closed": ["src/a.py"]}, "закрыт для правок"),
    ],
)
async def test_write_through_open_outside_rules_is_reverted(project, code, kwargs, reason):
    before = (project / "src" / "a.py").read_text()
    run = await run_script(project, code, **kwargs)
    assert reason in run.output
    assert (project / "src" / "a.py").read_text() == before
    assert not (project / "docs.md").exists()


async def test_run_tests_returns_result_and_branches(project):
    (project / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    (project / "test_bad.py").write_text("def test_bad():\n    assert False\n")
    code = (
        "bad = run_tests(['test_bad.py'])\nok = run_tests(['test_ok.py'])\n"
        "print(bad.ok, ok.ok, bad.code != 0)\n"
        "if ok.ok:\n    finish('зелёное')"
    )
    run = await run_script(project, code)
    assert run.output.splitlines()[0] == "False True True"
    assert run.control.tool == "finish"
    assert run.green is True


@pytest.mark.parametrize(
    ("command", "expected"),
    [(f"{sys.executable} -c 'print(5)'", "[код 0]\n5"), ("ls", "команда не разрешена")],
)
async def test_run_returns_result_for_allowed_commands(project, command, expected):
    run = await run_script(project, f"print(run({command!r}))")
    assert expected in run.output
