import pytest

from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.markup import task_paths
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec

LINE_HEAD = "**Менять можно только**"
FILES = {"README.md": "OLD\n", "src/app.py": "VALUE = 1\n", "docs/devlab/agent.md": "текст\n"}
PREFIX_MESSAGE = "нет изменений в src/: правок не сделано, задача не выполнена"
NO_LINE_MESSAGE = "нет изменений: правок не сделано, задача не выполнена"


def task_line(*paths: str) -> str:
    return f"\n\n{LINE_HEAD} {', '.join(f'`{path}`' for path in paths)}\n"


def changed_message(*paths: str) -> str:
    return f"нет изменений в {', '.join(paths)}: правок не сделано, задача не выполнена"


def make_guard(tmp_path, task, spec=None, edited=None):
    for name, text in FILES.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    sandbox = LocalSandbox(tmp_path)
    if edited is not None:
        (tmp_path / edited).write_text("NEW\n")
    return FinishGuard(sandbox, spec or AgentSpec(), task=task)


@pytest.mark.parametrize(
    "allowed,edited",
    [
        ("README.md", "README.md"),
        ("src/", "src/app.py"),
        ("src/*.py", "src/app.py"),
        ("docs/devlab/agent.md", "docs/devlab/agent.md"),
    ],
)
async def test_edit_in_allowed_path_accepted(tmp_path, allowed, edited):
    guard = make_guard(tmp_path, task_line(allowed), edited=edited)
    assert await guard.verify() == []


@pytest.mark.parametrize(
    "paths,edited",
    [
        (("README.md",), "src/app.py"),
        (("docs/devlab/agent.md",), "src/app.py"),
        (("README.md", "src/"), "docs/other.md"),
    ],
)
async def test_edit_outside_allowed_paths_rejected(tmp_path, paths, edited):
    guard = make_guard(tmp_path, task_line(*paths), edited=edited)
    assert await guard.verify() == [changed_message(*paths)]


async def test_edit_outside_paths_accepted_with_no_changes(tmp_path):
    spec = AgentSpec(allow_no_changes=True)
    guard = make_guard(tmp_path, task_line("README.md"), edited="src/app.py", spec=spec)
    assert await guard.verify() == []


async def test_paths_scoped_to_marked_line(tmp_path):
    task = f"цель `src/app.py`\n{task_line('README.md')}"
    guard = make_guard(tmp_path, task, edited="src/app.py")
    assert await guard.verify() == [changed_message("README.md")]


@pytest.mark.parametrize(
    "edited,accepted",
    [("src/app.py", True), ("README.md", False)],
)
async def test_no_line_keeps_diff_prefixes(tmp_path, edited, accepted):
    guard = make_guard(tmp_path, "цель", edited=edited, spec=AgentSpec(diff_prefixes=("src/",)))
    assert await guard.verify() == ([] if accepted else [PREFIX_MESSAGE])


async def test_no_line_no_diff_rejected(tmp_path):
    guard = make_guard(tmp_path, "цель")
    assert await guard.verify() == [NO_LINE_MESSAGE]


@pytest.mark.parametrize(
    "task,paths",
    [
        (f"{LINE_HEAD} `README.md`, `src/`.", ("README.md", "src/")),
        (f"`{LINE_HEAD}` `a.py`.", ()),
        ("просто цель", ()),
        (f"{LINE_HEAD} `docs/a.md:10-20`.", ("docs/a.md",)),
        (f"цель\n{LINE_HEAD} `a.md`.\n{LINE_HEAD} `b.md`.", ("a.md", "b.md")),
    ],
)
def test_task_paths(task, paths):
    assert task_paths(task) == paths
