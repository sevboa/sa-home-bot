import sys

import pytest

from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.script import worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import TOOL_EXECUTE, ToolBox

REFUSAL = "Сначала запиши черновик результата в plans/"


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
    (tmp_path / "plans").mkdir()
    return tmp_path


def toolbox(root, **spec):
    agent = AgentSpec(code_mode=True, write_prefixes=("plans/",), **spec)
    return ToolBox(LocalSandbox(root), agent)


async def run(box, code):
    return (await box.call(TOOL_EXECUTE, {"code": code})).text


async def test_reads_work_until_threshold_then_refused(project):
    box = toolbox(project, draft_after_reads=2)
    assert REFUSAL not in await run(box, "read('src/a.py')\nread('src/a.py')")
    for call in ("read('src/a.py')", "grep('value')", "ls()", "read_many(['src/a.py'])"):
        assert REFUSAL in await run(box, call)
    assert ": что знаешь уже сейчас, пусть неполный. Потом читай код и уточняй файл." in await run(
        box, "read('src/a.py')"
    )


async def test_read_many_counts_once(project):
    box = toolbox(project, draft_after_reads=2)
    assert REFUSAL not in await run(box, "read_many(['src/a.py', 'src/a.py', 'src/a.py'])")
    assert REFUSAL not in await run(box, "ls()")
    assert REFUSAL in await run(box, "ls()")


async def test_write_under_prefix_lifts_restriction(project):
    box = toolbox(project, draft_after_reads=1)
    await run(box, "read('src/a.py')")
    assert REFUSAL in await run(box, "read('src/a.py')")
    await run(box, "write('plans/p.md', 'черновик')")
    out = await run(box, "print(read('src/a.py'))\nprint(len(grep('value')))")
    assert REFUSAL not in out
    assert "value = 1" in out


async def test_edit_under_prefix_lifts_restriction(project):
    (project / "plans" / "p.md").write_text("старое\n")
    box = toolbox(project, draft_after_reads=1)
    await run(box, "read('src/a.py')")
    assert REFUSAL in await run(box, "read('src/a.py')")
    await run(box, "edit('plans/p.md', 'старое', 'новое')")
    assert REFUSAL not in await run(box, "read('src/a.py')")


async def test_zero_disables_limit(project):
    box = toolbox(project, draft_after_reads=0)
    code = "for _ in range(30):\n    read('src/a.py')\nprint('готово')"
    out = await run(box, code)
    assert REFUSAL not in out
    assert "готово" in out


async def test_without_write_prefixes_no_limit(project):
    box = ToolBox(LocalSandbox(project), AgentSpec(code_mode=True, draft_after_reads=1))
    out = await run(box, "read('src/a.py')\nread('src/a.py')\nprint('готово')")
    assert "готово" in out


async def test_write_outside_prefix_does_not_lift(project):
    box = toolbox(project, draft_after_reads=1)
    await run(box, "read('src/a.py')")
    attempt = "try:\n    write('src/b.py', 'x = 1')\nexcept EditError as e:\n    print(e)"
    out = await run(box, attempt)
    assert "вне путей роли" in out
    assert REFUSAL in await run(box, "read('src/a.py')")
