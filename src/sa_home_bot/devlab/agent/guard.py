from __future__ import annotations

import asyncio
from collections.abc import Callable

from sa_home_bot.devlab.agent.sandbox import (
    ExecResult,
    Sandbox,
    failed_test_ids,
    reported_failures,
)
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import changed_test_files, without_xdist

VERDICT_ACCEPTED = "ВЕРДИКТ: принято"
VERDICT_REWORK = "ВЕРДИКТ: доработать"
PYTEST_FAILED_EXIT = 1
BASE_RED_NOTE = "красные на базе, не блокируют: {tests}"
PhaseListener = Callable[[bool], None]


class FinishGuard:
    def __init__(
        self,
        sandbox: Sandbox,
        spec: AgentSpec,
        heavy: asyncio.Lock | None = None,
        on_phase: PhaseListener | None = None,
    ) -> None:
        self._sandbox = sandbox
        self._spec = spec
        self._heavy = heavy or asyncio.Lock()
        self._on_phase = on_phase
        self._base_red: set[str] = set()
        self._excused: list[str] = []

    @property
    def excused(self) -> list[str]:
        return list(self._excused)

    async def verify(self) -> list[str]:
        spec = self._spec
        changed = await self._sandbox.changed_paths()
        if spec.diff_prefixes:
            changed = [p for p in changed if p.startswith(spec.diff_prefixes)]
        if not changed:
            if spec.allow_no_changes:
                return await self.verify_green()
            where = f" в {', '.join(spec.diff_prefixes)}" if spec.diff_prefixes else ""
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
        if self._on_phase is not None:
            self._on_phase(True)
        try:
            async with self._heavy:
                if spec.test_argv:
                    result = await self._sandbox.exec(spec.test_argv, spec.test_timeout_s)
                    if not result.ok and not await self._red_only_on_base(result):
                        failures.append(
                            "тесты не зелёные:\n"
                            + result.failure_summary(spec.output_chars, spec.test_timeout_s)
                        )
                failures += await self._checks()
        finally:
            if self._on_phase is not None:
                self._on_phase(False)
        return failures

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
