from __future__ import annotations

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.epic_plan import PlanTask, check_plan, plan_test_files


def task(title="t", text="сделать", paths=("src/a.py",), depends_on=()):
    return PlanTask(title, text, "m", paths=tuple(paths), depends_on=tuple(depends_on))


def test_valid_plan_has_no_reasons():
    plan = [
        task("a", paths=["src/a.py", "tests/unit/test_a.py"]),
        task("b", paths=["src/b.py", "tests/unit/test_b.py"], depends_on=[0]),
    ]
    assert check_plan(plan, ["tests/unit/test_old.py"]) == []


@pytest.mark.parametrize(
    ("plan", "existing", "fragment"),
    [
        ([task(text="x" * (protocol.TASK_LONG_CHARS + 1))], [], "знаков"),
        ([task(depends_on=[0])], [], "цикл"),
        ([task(), task(depends_on=[2]), task(depends_on=[1])], [], "цикл"),
        ([task(depends_on=[1]), task()], [], "ссылка вперёд"),
        ([task(depends_on=[5])], [], "нет в плане"),
        ([task(depends_on=[-1])], [], "нет в плане"),
        ([task(paths=[])], [], "paths пуст"),
        ([task(paths=["../x.py"])], [], "абсолютный или с .."),
        ([task(paths=["/etc/x.py"])], [], "абсолютный или с .."),
        (
            [task(paths=["tests/unit/test_a.py"]), task(paths=["tests/other/test_a.py"])],
            [],
            "тест-файл test_a.py",
        ),
        (
            [task(paths=["tests/unit/test_a.py"]), task(paths=["tests/unit/test_a.py"])],
            [],
            "тест-файл test_a.py",
        ),
        ([task(paths=["tests/new/test_old.py"])], ["tests/unit/test_old.py"], "уже есть в базе"),
    ],
)
def test_each_refusal_reason(plan, existing, fragment):
    reasons = check_plan(plan, existing)
    assert any(fragment in reason for reason in reasons), reasons


def test_reasons_number_tasks_like_depends_on():
    reasons = check_plan([task("a"), task("b", depends_on=[1])])
    expected = "задача 1 (b): цикл через задачу 1"
    assert any(expected in reason for reason in reasons), reasons


def test_existing_test_path_may_be_edited():
    assert check_plan([task(paths=["tests/unit/test_old.py"])], ["tests/unit/test_old.py"]) == []


def test_same_file_listed_twice_in_one_task_is_fine():
    assert check_plan([task(paths=["tests/unit/test_a.py", "tests/unit/test_a.py"])]) == []


def test_all_reasons_are_listed_together():
    plan = [task(paths=[], depends_on=[3]), task(text="x" * 2000)]
    assert len(check_plan(plan)) == 3


def test_plan_test_files_only_matches_tests_dir_and_pattern():
    paths = ["tests/unit/test_a.py", "tests/unit/helper.py", "src/test_b.py", "tests/conftest.py"]
    assert plan_test_files(paths) == ["tests/unit/test_a.py"]
