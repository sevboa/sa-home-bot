from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath

from sa_home_bot.devlab.agent.sandbox import Sandbox
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import without_xdist


def changed_test_files(digests: Mapping[str, str]) -> list[str]:
    return sorted(
        path
        for path, digest in digests.items()
        if digest and _is_test_file(PurePosixPath(path).name)
    )


def _is_test_file(name: str) -> bool:
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


class FinishGuard:
    def __init__(self, sandbox: Sandbox, spec: AgentSpec) -> None:
        self._sandbox = sandbox
        self._spec = spec

    async def verify(self) -> list[str]:
        spec = self._spec
        changed = await self._sandbox.changed_paths()
        if spec.diff_prefixes:
            changed = [p for p in changed if p.startswith(spec.diff_prefixes)]
        if not changed:
            where = f" в {', '.join(spec.diff_prefixes)}" if spec.diff_prefixes else ""
            return [f"нет изменений{where}: правок не сделано, задача не выполнена"]
        return await self.verify_green()

    async def verify_green(self) -> list[str]:
        spec = self._spec
        failures: list[str] = []
        if spec.test_argv:
            result = await self._sandbox.exec(spec.test_argv, spec.test_timeout_s)
            if not result.ok:
                failures.append(
                    "тесты не зелёные:\n" + result.summary(spec.output_chars, spec.test_timeout_s)
                )
        return failures + await self._checks()

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
                    + result.summary(spec.output_chars, spec.narrow_test_timeout_s)
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
