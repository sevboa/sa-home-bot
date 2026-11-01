from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from sa_home_bot.utils.process import run

IGNORED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
NOT_FOUND_EXIT_CODE = 127
TIMEOUT_EXIT_CODE = -1


class SandboxPathError(Exception):
    pass


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self, limit: int, timeout_s: float = 0.0) -> str:
        text = self.output
        if len(text) > limit:
            text = f"[показан конец: {limit} симв. из {len(text)}]\n{text[-limit:]}"
        head = f"[таймаут {timeout_s:g} с]" if self.timed_out else f"[код {self.exit_code}]"
        return f"{head}\n{text}".rstrip()


class Sandbox(Protocol):
    async def read_bytes(self, path: str) -> bytes | None: ...

    async def write_bytes(self, path: str, data: bytes) -> None: ...

    async def list_files(self) -> list[str]: ...

    async def exec(self, argv: Sequence[str], timeout_s: float) -> ExecResult: ...

    async def changed_paths(self) -> list[str]: ...


def safe_relative(path: str) -> str:
    pure = PurePosixPath(path.strip())
    if not path.strip() or pure.is_absolute() or ".." in pure.parts:
        raise SandboxPathError(f"путь {path!r} недопустим: нужен относительный путь внутри проекта")
    return str(pure)


class LocalSandbox:
    def __init__(self, root: Path, baseline: dict[str, str] | None = None) -> None:
        self._root = root
        self._baseline = baseline if baseline is not None else self._snapshot()

    def _walk(self) -> list[str]:
        found = []
        for path in self._root.rglob("*"):
            relative = path.relative_to(self._root)
            if path.is_file() and not IGNORED_DIRS.intersection(relative.parts):
                found.append(relative.as_posix())
        return sorted(found)

    def _snapshot(self) -> dict[str, str]:
        return {
            rel: hashlib.sha256((self._root / rel).read_bytes()).hexdigest() for rel in self._walk()
        }

    def snapshot(self) -> dict[str, str]:
        return self._snapshot()

    def _resolve(self, path: str) -> Path:
        return self._root / safe_relative(path)

    async def read_bytes(self, path: str) -> bytes | None:
        target = self._resolve(path)
        return target.read_bytes() if target.is_file() else None

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def list_files(self) -> list[str]:
        return self._walk()

    async def exec(self, argv: Sequence[str], timeout_s: float) -> ExecResult:
        try:
            result = await run(
                list(argv),
                timeout=timeout_s,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._root),
            )
        except TimeoutError:
            return ExecResult(TIMEOUT_EXIT_CODE, "", timed_out=True)
        except OSError as exc:
            return ExecResult(NOT_FOUND_EXIT_CODE, f"{type(exc).__name__}: {exc}")
        return ExecResult(result.returncode, result.stdout_text)

    async def changed_paths(self) -> list[str]:
        current = self._snapshot()
        changed = {rel for rel, digest in current.items() if self._baseline.get(rel) != digest}
        return sorted(changed | (self._baseline.keys() - current.keys()))
