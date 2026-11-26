import json

from sa_home_bot.devlab.agent.guard import PLAN_HINT, FinishGuard
from sa_home_bot.devlab.agent.sandbox import LocalSandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.protocol import TASK_LONG_CHARS

PLAN = "plans/epic.json"


def good_plan():
    return [
        {
            "title": "первая",
            "task": "Сделать X, тест станет зелёным.",
            "profile": "developer",
            "paths": ["src/x.py", "tests/unit/test_x.py"],
            "depends_on": [],
        },
        {
            "title": "вторая",
            "task": "Сделать Y.",
            "profile": "developer",
            "paths": ["src/y.py", "tests/unit/test_y.py"],
            "depends_on": [0],
        },
    ]


def make_guard(tmp_path, content, plan_check=True):
    sandbox = LocalSandbox(tmp_path)
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "epic.json").write_text(
        content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    )
    spec = AgentSpec(write_prefixes=("plans/",), plan_check=plan_check)
    return FinishGuard(sandbox, spec, task="план")


async def test_good_plan_accepted(tmp_path):
    assert await make_guard(tmp_path, good_plan()).verify() == []


async def test_long_task_rejected_with_reason(tmp_path):
    plan = good_plan()
    plan[0]["task"] = "я" * (TASK_LONG_CHARS + 1)
    failures = await make_guard(tmp_path, plan).verify()
    assert len(failures) == 1
    assert f"> {TASK_LONG_CHARS}" in failures[0]
    assert failures[0].endswith(PLAN_HINT)


async def test_dependency_reason_without_split_hint(tmp_path):
    plan = good_plan()
    plan[1]["depends_on"] = [1]
    failures = await make_guard(tmp_path, plan).verify()
    assert failures == ["план задача 1 (вторая): цикл через задачу 1"]


async def test_broken_json_rejected(tmp_path):
    failures = await make_guard(tmp_path, "{не json").verify()
    assert len(failures) == 1
    assert PLAN in failures[0]
    assert "не JSON" in failures[0]


async def test_not_list_of_objects_rejected(tmp_path):
    failures = await make_guard(tmp_path, [1, 2]).verify()
    assert "не объект" in failures[0]


async def test_missing_fields_rejected(tmp_path):
    failures = await make_guard(tmp_path, [{"title": "a"}]).verify()
    assert "нет полей task, paths" in failures[0]


async def test_existing_test_file_clash_rejected(tmp_path):
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/unit/test_x.py").write_text("")
    plan = good_plan()
    plan[0]["paths"] = ["tests/other/test_x.py"]
    guard = make_guard(tmp_path, plan)
    failures = await guard.verify()
    assert any("уже есть в базе" in f for f in failures)


async def test_without_plan_check_not_validated(tmp_path):
    assert await make_guard(tmp_path, "{не json", plan_check=False).verify() == []
