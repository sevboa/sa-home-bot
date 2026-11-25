from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import Callable
from pathlib import PurePosixPath

from sa_home_bot.devlab.agent.markup import task_paths
from sa_home_bot.devlab.agent.sandbox import (
    ExecResult,
    Sandbox,
    failed_test_ids,
    reported_failures,
)
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import changed_test_files, without_xdist
from sa_home_bot.devlab.hub.epic_plan import PlanTask, check_plan, plan_test_files
from sa_home_bot.devlab.protocol import RED_TESTS_ITEM, RED_TESTS_MARKER

VERDICT_ACCEPTED = "ВЕРДИКТ: принято"
VERDICT_REWORK = "ВЕРДИКТ: доработать"
PYTEST_FAILED_EXIT = 1
BASE_RED_NOTE = "красные на базе, не блокируют: {tests}"
PhaseListener = Callable[[bool], None]
PLANS_DIR = "plans/"
PLAN_SUFFIX = ".json"
PLAN_HINT = (
    "Задача говорит, что должно работать и какой тест станет зелёным; "
    "длинную — раздели на несколько"
)
PLAN_FIELDS = ("title", "task", "paths")
COLLECT_ARGS = ("--collect-only", "-q")
COLLECT_BROKEN = (
    "тест не собирается (ошибка на уровне модуля), а тесты до кода обязаны собираться:\n"
    "{output}\n"
    "Правило: на уровне модуля импортируй только то, что уже есть в репозитории; ещё не "
    "написанный код импортируй внутри тела тестовой функции (`from пакет import новое`). "
    "Тогда без кода тест падает красным, а не ломает сбор. Синтаксис должен быть корректным."
)


def red_tests_block(tests: list[str]) -> str:
    if not tests:
        return ""
    return "\n\n" + RED_TESTS_MARKER + "\n" + "\n".join(RED_TESTS_ITEM + test for test in tests)


def _plan_tasks(name: str, raw: bytes) -> tuple[list[PlanTask], str]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return [], f"{name}: не JSON ({exc})"
    if not isinstance(data, list) or not data:
        return [], f"{name}: ожидается непустой JSON-список задач"
    tasks = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            return [], f"{name}: элемент {index + 1} не объект"
        missing = [key for key in PLAN_FIELDS if key not in item]
        if missing:
            return [], f"{name}: в элементе {index + 1} нет полей {', '.join(missing)}"
        paths, deps = item["paths"], item.get("depends_on", [])
        if not isinstance(item["title"], str) or not isinstance(item["task"], str):
            return [], f"{name}: в элементе {index + 1} title и task должны быть строками"
        if not _is_list_of(paths, str) or not _is_list_of(deps, int):
            return [], f"{name}: в элементе {index + 1} paths и depends_on должны быть списками"
        fields = {"profile": item["profile"]} if isinstance(item.get("profile"), str) else {}
        tasks.append(
            PlanTask(
                title=item["title"],
                task=item["task"],
                model="",
                paths=tuple(paths),
                depends_on=tuple(deps),
                **fields,
            )
        )
    return tasks, ""


def _is_list_of(value: object, kind: type) -> bool:
    return isinstance(value, list) and all(
        isinstance(v, kind) and not isinstance(v, bool) for v in value
    )


def _in_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    return any(
        path.startswith(item) if item.endswith("/") else fnmatch.fnmatch(path, item)
        for item in allowed
    )


class FinishGuard:
    def __init__(
        self,
        sandbox: Sandbox,
        spec: AgentSpec,
        heavy: asyncio.Lock | None = None,
        on_phase: PhaseListener | None = None,
        task: str = "",
    ) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._task = task
        self._heavy = heavy or asyncio.Lock()
        self._on_phase = on_phase
        self._base_red: set[str] = set()
        self._excused: list[str] = []
        self._red: list[str] = []

    @property
    def excused(self) -> list[str]:
        return list(self._excused)

    @property
    def red_tests(self) -> list[str]:
        return list(self._red)

    async def _own_test_files(self) -> list[str]:
        if not self._spec.expect_red:
            return []
        return changed_test_files(await self._sandbox.changed_digests())

    async def _uncollectable(self, files: list[str]) -> list[str]:
        spec = self._spec
        argv = without_xdist([*spec.test_argv, *COLLECT_ARGS, *files])
        result = await self._sandbox.exec(argv, spec.narrow_test_timeout_s)
        if result.ok:
            return []
        return [
            COLLECT_BROKEN.format(
                output=result.failure_summary(spec.output_chars, spec.narrow_test_timeout_s)
            )
        ]

    async def _record_red(self, files: list[str]) -> None:
        spec = self._spec
        argv = without_xdist([*spec.test_argv, *files])
        result = await self._sandbox.exec(argv, spec.test_timeout_s)
        if result.ok:
            self._red = []
            return
        self._red = failed_test_ids(result.output) or list(files)

    async def _plan_failures(self, changed: list[str]) -> list[str]:
        plans = [
            p
            for p in changed
            if p.startswith(PLANS_DIR)
            and PurePosixPath(p).parent.as_posix() + "/" == PLANS_DIR
            and p.endswith(PLAN_SUFFIX)
        ]
        if not plans:
            return []
        existing = plan_test_files(await self._sandbox.list_files())
        failures: list[str] = []
        for path in plans:
            raw = await self._sandbox.read_bytes(path)
            if raw is None:
                continue
            tasks, error = _plan_tasks(path, raw)
            reasons = [error] if error else check_plan(tasks, existing)
            failures += [f"план {reason}. {PLAN_HINT}" for reason in reasons]
        return failures

    async def verify(self) -> list[str]:
        spec = self._spec
        changed = await self._sandbox.changed_paths()
        allowed = task_paths(self._task)
        if allowed:
            changed = [p for p in changed if _in_allowed(p, allowed)]
        elif spec.write_prefixes:
            changed = [p for p in changed if p.startswith(spec.write_prefixes)]
        elif spec.diff_prefixes:
            changed = [p for p in changed if p.startswith(spec.diff_prefixes)]
        if spec.plan_check:
            plan_failures = await self._plan_failures(changed)
            if plan_failures:
                return plan_failures
        if not changed:
            if spec.allow_no_changes:
                return await self.verify_green()
            scope = allowed or spec.write_prefixes or spec.diff_prefixes
            where = f" в {', '.join(scope)}" if scope else ""
            return [f"нет изменений{where}: правок не сделано, задача не выполнена"]
        return await self.verify_green()

    def verify_summary(self, summary: str) -> list[str]:
        spec = self._spec
        if not spec.require_verdict:
            return []
        if any(
            line.strip().startswith((VERDICT_ACCEPTED, VERDICT_REWORK))
            for line in summary.splitlines()
        ):
            return []
        return [
            "нет вердикта: добавь в summary строку "
            "«ВЕРДИКТ: принято» или «ВЕРДИКТ: доработать» и пункты замечаний"
        ]

    async def verify_green(self) -> list[str]:
        spec = self._spec
        failures: list[str] = []
        self._excused = []
        self._red = []
        if self._on_phase is not None:
            self._on_phase(True)
        try:
            async with self._heavy:
                own = await self._own_test_files() if spec.test_argv else []
                if own:
                    failures += await self._uncollectable(own)
                if spec.test_argv:
                    ignored = [f"--ignore={path}" for path in own]
                    argv = [*spec.test_argv, *await self._skip_args(), *ignored]
                    result = await self._sandbox.exec(argv, spec.test_timeout_s)
                    if not result.ok and not await self._red_only_on_base(result):
                        failures.append(
                            "тесты не зелёные:\n"
                            + result.failure_summary(spec.output_chars, spec.test_timeout_s)
                        )
                if own and not failures:
                    await self._record_red(own)
                failures += await self._checks()
        finally:
            if self._on_phase is not None:
                self._on_phase(False)
        return failures

    async def _skip_args(self) -> list[str]:
        existing = [
            path
            for path in self._spec.skip_tests
            if await self._sandbox.read_bytes(path) is not None
        ]
        return [f"--ignore={path}" for path in existing]

    async def _red_only_on_base(self, result: ExecResult) -> bool:
        if result.timed_out or result.exit_code != PYTEST_FAILED_EXIT:
            return False
        ids = failed_test_ids(result.output)
        if not ids or len(ids) != reported_failures(result.output):
            return False
        touched = set(await self._sandbox.changed_paths())
        if any(test_id.split("::")[0] in touched for test_id in ids):
            return False
        unknown = [test_id for test_id in ids if test_id not in self._base_red]
        if unknown:
            spec = self._spec
            argv = without_xdist([*spec.test_argv, *unknown])
            base = await self._sandbox.exec_on_base(argv, spec.test_timeout_s)
            if base is None or base.timed_out or base.exit_code != PYTEST_FAILED_EXIT:
                return False
            self._base_red |= set(failed_test_ids(base.output)) & set(unknown)
        if not all(test_id in self._base_red for test_id in ids):
            return False
        self._excused = ids
        return True

    async def verify_step(self) -> list[str]:
        spec = self._spec
        failures: list[str] = []
        paths = changed_test_files(await self._sandbox.changed_digests())
        if spec.expect_red and spec.test_argv and paths:
            failures += await self._uncollectable(paths)
            return failures + await self._checks()
        if spec.test_argv and paths:
            argv = without_xdist([*spec.test_argv, *paths])
            result = await self._sandbox.exec(argv, spec.narrow_test_timeout_s)
            if not result.ok:
                failures.append(
                    "изменённые тесты не зелёные:\n"
                    + result.failure_summary(spec.output_chars, spec.narrow_test_timeout_s)
                )
        return failures + await self._checks()

    async def _checks(self) -> list[str]:
        spec = self._spec
        failures: list[str] = []
        for argv in spec.checks:
            result = await self._sandbox.exec(argv, spec.command_timeout_s)
            if not result.ok:
                failures.append(
                    f"страж {' '.join(argv)} красный:\n"
                    + result.summary(spec.output_chars, spec.command_timeout_s)
                )
        return failures
