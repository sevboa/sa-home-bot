from __future__ import annotations

from sa_home_bot.devlab.agent.sandbox import Sandbox
from sa_home_bot.devlab.agent.spec import AgentSpec


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
        for argv in spec.checks:
            result = await self._sandbox.exec(argv, spec.command_timeout_s)
            if not result.ok:
                failures.append(
                    f"страж {' '.join(argv)} красный:\n"
                    + result.summary(spec.output_chars, spec.command_timeout_s)
                )
        return failures
