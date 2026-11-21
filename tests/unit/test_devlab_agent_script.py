import sys

import pytest

from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import (
    ACTION_EDIT,
    ACTION_RUN_TESTS,
    ACTION_WRITE,
    ScriptAction,
    parse_output,
    script_argv,
)


async def run_script(root, code):
    result = await LocalSandbox(root).exec(script_argv(code, python=sys.executable), 30)
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
    assert run.actions == ()
    assert "1\tbeta = 20" in run.output
    assert "['src/a.py', 'src/b.py']" in run.output


async def test_grep_with_context_marks_match_and_neighbours(project):
    run = await run_script(project, "print('\\n'.join(grep('^beta', 'src/a', context=1)))")
    assert run.output.splitlines() == [
        "src/a.py-1-alpha = 1",
        "src/a.py:2:beta = 2",
        "src/a.py-3-gamma = 3",
    ]


@pytest.mark.parametrize(
    ("code", "action"),
    [
        (
            "edit('src/a.py', 'alpha', 'omega')",
            ScriptAction(ACTION_EDIT, {"path": "src/a.py", "old": "alpha", "new": "omega"}),
        ),
        (
            "write('src/c.py', 'x = 1\\n')",
            ScriptAction(ACTION_WRITE, {"path": "src/c.py", "content": "x = 1\n"}),
        ),
        (
            "run_tests(['tests/a.py', 'tests/b.py'])",
            ScriptAction(ACTION_RUN_TESTS, {"paths": "tests/a.py tests/b.py"}),
        ),
    ],
)
async def test_mutations_are_queued_not_applied(project, code, action):
    before = (project / "src" / "a.py").read_text()
    run = await run_script(project, code)
    assert run.actions == (action,)
    assert not run.failed
    assert (project / "src" / "a.py").read_text() == before
    assert not (project / "src" / "c.py").exists()


async def test_exception_marks_script_failed_with_traceback(project):
    run = await run_script(project, "edit('src/a.py', 'a', 'b')\nraise LookupError('нет')")
    assert run.failed
    assert "Traceback" in run.output
    assert "LookupError: нет" in run.output


@pytest.mark.parametrize("text", ["просто вывод\n", "DEVLAB_ACTIONS {битый"])
def test_output_without_valid_marker_is_failed(text):
    run = parse_output(text)
    assert run.failed
    assert run.actions == ()
    assert run.output == text.rstrip()
