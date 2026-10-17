from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import IO

Stream = int | IO[bytes] | None


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode(errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode(errors="replace").strip()


async def run(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float | None = None,
    input: bytes | None = None,  # noqa: A002
    stdout: Stream = asyncio.subprocess.PIPE,
    stderr: Stream = asyncio.subprocess.PIPE,
) -> ProcResult:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if input is not None else None,
        stdout=stdout,
        stderr=stderr,
    )
    exchange = proc.communicate(input)
    if timeout is not None:
        exchange = asyncio.wait_for(exchange, timeout=timeout)
    out, err = await exchange
    return ProcResult(proc.returncode or 0, out or b"", err or b"")
