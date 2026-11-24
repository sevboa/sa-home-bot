from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import PurePosixPath

from sa_home_bot.devlab import protocol

TESTS_DIR = "tests"
TEST_FILE_PATTERN = "test_*.py"


@dataclass(frozen=True)
class PlanTask:
    title: str
    task: str
    model: str
    profile: str = protocol.DEFAULT_PROFILE
    paths: tuple[str, ...] = ()
    depends_on: tuple[int, ...] = field(default_factory=tuple)


def plan_test_files(paths: Collection[str]) -> list[str]:
    result = []
    for path in paths:
        pure = PurePosixPath(path)
        if pure.parts[:1] == (TESTS_DIR,) and fnmatchcase(pure.name, TEST_FILE_PATTERN):
            result.append(path)
    return result


def _label(index: int, task: PlanTask) -> str:
    return f"задача {index + 1} ({task.title or 'без названия'})"


def _length_reasons(tasks: Sequence[PlanTask]) -> list[str]:
    return [
        f"{_label(i, t)}: task {len(t.task)} знаков > {protocol.TASK_LONG_CHARS}"
        for i, t in enumerate(tasks)
        if len(t.task) > protocol.TASK_LONG_CHARS
    ]


def _path_reasons(tasks: Sequence[PlanTask]) -> list[str]:
    reasons = []
    for i, t in enumerate(tasks):
        if not t.paths:
            reasons.append(f"{_label(i, t)}: paths пуст")
            continue
        for path in t.paths:
            pure = PurePosixPath(path)
            if not path.strip() or pure.is_absolute() or ".." in pure.parts:
                reasons.append(f"{_label(i, t)}: путь {path!r} пуст, абсолютный или с ..")
    return reasons


def _graph_reasons(tasks: Sequence[PlanTask]) -> list[str]:
    reasons = []
    valid: dict[int, list[int]] = {}
    for i, t in enumerate(tasks):
        valid[i] = []
        for dep in t.depends_on:
            if not 0 <= dep < len(tasks):
                reasons.append(f"{_label(i, t)}: зависимости {dep + 1} нет в плане")
            else:
                valid[i].append(dep)
                if dep > i:
                    reasons.append(f"{_label(i, t)}: ссылка вперёд на задачу {dep + 1}")
    reasons.extend(f"{_label(i, tasks[i])}: цикл через задачу {i + 1}" for i in _in_cycle(valid))
    return reasons


def _in_cycle(graph: dict[int, list[int]]) -> list[int]:
    cyclic = []
    for start in graph:
        seen: set[int] = set()
        stack = list(graph[start])
        while stack:
            node = stack.pop()
            if node == start:
                cyclic.append(start)
                break
            if node not in seen:
                seen.add(node)
                stack.extend(graph.get(node, ()))
    return cyclic


def _test_reasons(tasks: Sequence[PlanTask], existing: Collection[str]) -> list[str]:
    reasons = []
    owner: dict[str, tuple[int, str]] = {}
    existing_by_name: dict[str, set[str]] = {}
    for path in existing:
        existing_by_name.setdefault(PurePosixPath(path).name, set()).add(path)
    for i, t in enumerate(tasks):
        for path in dict.fromkeys(plan_test_files(t.paths)):
            name = PurePosixPath(path).name
            if name in owner and owner[name][0] != i:
                reasons.append(
                    f"тест-файл {name}: в задаче {owner[name][0] + 1} ({owner[name][1]}) и в "
                    f"задаче {i + 1} ({path})"
                )
            owner.setdefault(name, (i, path))
            clash = existing_by_name.get(name, set()) - {path}
            if clash and path not in existing:
                reasons.append(
                    f"{_label(i, t)}: тест-файл {name} уже есть в базе: {', '.join(sorted(clash))}"
                )
    return reasons


def check_plan(tasks: Sequence[PlanTask], existing_tests: Collection[str] = ()) -> list[str]:
    return [
        *_length_reasons(tasks),
        *_graph_reasons(tasks),
        *_path_reasons(tasks),
        *_test_reasons(tasks, existing_tests),
    ]
