from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSpec:
    test_argv: tuple[str, ...] = ()
    checks: tuple[tuple[str, ...], ...] = ()
    allowed_commands: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    diff_prefixes: tuple[str, ...] = ()
    command_timeout_s: float = 300.0
    test_timeout_s: float = 3600.0
    output_chars: int = 6000
