import sys

import pytest

from sa_home_bot.devlab.agent.guard import FinishGuard, red_tests_block
from sa_home_bot.devlab.agent.markup import task_paths
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.hub.epic_plan import parse_red_tests

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


@pytest.mark.parametrize(
    "edited,accepted",
    [("docs/devlab/agent.md", True), ("src/app.py", False)],
)
async def test_write_prefixes_scope_finish(tmp_path, edited, accepted):
    spec = AgentSpec(write_prefixes=("docs/",), diff_prefixes=("src/",))
    guard = make_guard(tmp_path, "цель", edited=edited, spec=spec)
    assert await guard.verify() == ([] if accepted else [changed_message("docs/")])


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


class RecordingSandbox(LocalSandbox):
    def __init__(self, root):
        super().__init__(root)
        self.argvs = []

    async def exec(self, argv, timeout_s):
        self.argvs.append(list(argv))
        return await super().exec(["true"], timeout_s)


async def test_full_run_ignores_only_existing_skipped_tests(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_later.py").write_text("")
    sandbox = RecordingSandbox(tmp_path)
    spec = AgentSpec(
        test_argv=("pytest", "-q"),
        skip_tests=("tests/test_later.py", "tests/test_missing.py"),
    )
    assert await FinishGuard(sandbox, spec).verify_green() == []
    assert sandbox.argvs == [["pytest", "-q", "--ignore=tests/test_later.py"]]


async def test_full_run_without_skip_keeps_argv(tmp_path):
    sandbox = RecordingSandbox(tmp_path)
    await FinishGuard(sandbox, AgentSpec(test_argv=("pytest", "-q"))).verify_green()
    assert sandbox.argvs == [["pytest", "-q"]]


PYTEST = (sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider")
GREEN_TEST = "def test_ok():\n    assert True\n"
RED_TEST = "def test_new():\n    from newmod import value\n\n    assert value == 1\n"
BROKEN_TEST = "from newmod import value\n\n\ndef test_new():\n    assert value == 1\n"


def red_sandbox(tmp_path, name, text):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_base.py").write_text(GREEN_TEST)
    sandbox = LocalSandbox(tmp_path)
    (tmp_path / name).write_text(text)
    return sandbox


async def test_expect_red_allows_red_own_test_and_lists_it(tmp_path):
    sandbox = red_sandbox(tmp_path, "tests/test_new.py", RED_TEST)
    guard = FinishGuard(sandbox, AgentSpec(test_argv=PYTEST, expect_red=True))
    assert await guard.verify_green() == []
    assert guard.red_tests == ["tests/test_new.py::test_new"]


async def test_without_expect_red_own_red_test_blocks(tmp_path):
    sandbox = red_sandbox(tmp_path, "tests/test_new.py", RED_TEST)
    failures = await FinishGuard(sandbox, AgentSpec(test_argv=PYTEST)).verify_green()
    assert failures and failures[0].startswith("тесты не зелёные")


async def test_expect_red_rejects_module_level_import_of_missing_code(tmp_path):
    sandbox = red_sandbox(tmp_path, "tests/test_new.py", BROKEN_TEST)
    guard = FinishGuard(sandbox, AgentSpec(test_argv=PYTEST, expect_red=True))
    failures = await guard.verify_green()
    assert len(failures) == 1
    assert "внутри тела тестовой функции" in failures[0]
    assert guard.red_tests == []


async def test_expect_red_keeps_rest_of_suite_required_green(tmp_path):
    sandbox = red_sandbox(tmp_path, "tests/test_new.py", RED_TEST)
    (tmp_path / "tests/test_base.py").write_text("def test_ok():\n    assert False\n")
    guard = FinishGuard(sandbox, AgentSpec(test_argv=PYTEST, expect_red=True))
    failures = await guard.verify_green()
    assert len(failures) == 1
    assert failures[0].startswith("тесты не зелёные")


async def test_expect_red_step_only_collects(tmp_path):
    sandbox = red_sandbox(tmp_path, "tests/test_new.py", RED_TEST)
    spec = AgentSpec(test_argv=PYTEST, expect_red=True)
    assert await FinishGuard(sandbox, spec).verify_step() == []


def test_red_tests_block_roundtrip():
    block = red_tests_block(["tests/a.py::t1", "tests/b.py::t2"])
    assert parse_red_tests("итог" + block + "\n\nхвост") == ["tests/a.py::t1", "tests/b.py::t2"]
    assert red_tests_block([]) == ""
    assert parse_red_tests("без маркера") == []
