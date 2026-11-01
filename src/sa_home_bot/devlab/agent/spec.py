from __future__ import annotations

import shlex
from dataclasses import dataclass

from sa_home_bot.devlab.executor.spec import (
    DEFAULT_COMMAND_TIMEOUT_S,
    DEFAULT_OUTPUT_CHARS,
    ProjectSpec,
)

DEFAULT_TEST_TIMEOUT_S = 3600.0


@dataclass(frozen=True)
class AgentSpec:
    test_argv: tuple[str, ...] = ()
    checks: tuple[tuple[str, ...], ...] = ()
    allowed_commands: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    secret_paths: tuple[str, ...] = ()
    diff_prefixes: tuple[str, ...] = ()
    command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S
    test_timeout_s: float = DEFAULT_TEST_TIMEOUT_S
    output_chars: int = DEFAULT_OUTPUT_CHARS

    @classmethod
    def from_project(cls, project: ProjectSpec) -> AgentSpec:
        section = project.agent
        test = project.test
        return cls(
            test_argv=tuple(shlex.split(test.cmd)) + tuple(test.args) if test.cmd.strip() else (),
            checks=tuple(tuple(shlex.split(check)) for check in section.checks if check.strip()),
            allowed_commands=tuple(section.allowed_commands),
            forbidden_paths=tuple(section.forbidden_paths),
            secret_paths=tuple(section.secret_paths),
            diff_prefixes=tuple(section.diff_prefixes),
            command_timeout_s=section.command_timeout_s,
            test_timeout_s=test.timeout_s,
            output_chars=section.output_chars,
        )
