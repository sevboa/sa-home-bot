from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

JUNIT_FILE = ".devlab-junit.xml"


@dataclass(frozen=True)
class RunRequest:
    job_id: str
    workdir: Path
    argv: tuple[str, ...]
    timeout_s: float
    image: str = ""
    setup: tuple[str, ...] = field(default_factory=tuple)
    setup_files: tuple[str, ...] = field(default_factory=tuple)
    env: tuple[tuple[str, str], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    output: bytes
    timed_out: bool = False


class TestRunner(Protocol):
    __test__ = False

    async def run(self, request: RunRequest) -> RunResult: ...

    async def cancel(self, job_id: str) -> None: ...
