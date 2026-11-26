from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from sa_home_bot.utils.process import run

IGNORED_DIRS = frozenset({".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
NOT_FOUND_EXIT_CODE = 127
TIMEOUT_EXIT_CODE = -1
EXIT_HEAD = "[код {code}]"
OK_HEAD = EXIT_HEAD.format(code=0)
SESSION_TICK_S = 0.5
SESSION_CLOSE_GRACE_S = 2.0
BINARY_MARK = "[файл не текстовый]"
CALL_KEY = "call"
CALL_UNAVAILABLE = "вызов хоста из скрипта недоступен"
CALL_FAILED = "сбой хоста при обработке вызова: {error}"
FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) \S")
SUMMARY_COUNT = re.compile(r"(\d+) (?:failed|error)")
FAILED_LIST_MAX = 30
FAILED_LINE_CHARS = 240
FAILED_LIST_HEAD = "упавшие тесты:"
FAILED_TAIL_HEAD = "хвост вывода:"
CallHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class SandboxPathError(Exception):
    pass


def clip_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"[показан конец: {limit} симв. из {len(text)}]\n{text[-limit:]}"


def frame(data: bytes) -> bytes:
    return f"{len(data)}\n".encode() + data


def call_request(data: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(data)
    except ValueError:
        return None
    if isinstance(payload, dict) and CALL_KEY in payload and "output" not in payload:
        return payload
    return None


def failed_tests(output: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in output.splitlines():
        if FAILED_LINE.match(line):
            seen.setdefault(line[:FAILED_LINE_CHARS], None)
    return list(seen)[:FAILED_LIST_MAX]


def failed_test_ids(output: str) -> list[str]:
    seen: dict[str, None] = {}
    for line in output.splitlines():
        if FAILED_LINE.match(line):
            seen.setdefault(line.split(" ", 1)[1].split(" - ", 1)[0].rstrip(), None)
    return list(seen)


def reported_failures(output: str) -> int:
    last = output.rstrip().splitlines()[-1] if output.strip() else ""
    return sum(int(n) for n in SUMMARY_COUNT.findall(last))


def clip_head(text: str, limit: int, hint: str = "") -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[обрезано: показано начало {limit} симв. из {len(text)}{hint}]"


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def summary(self, limit: int, timeout_s: float = 0.0) -> str:
        text = clip_tail(self.output, limit)
        head = (
            f"[таймаут {timeout_s:g} с]"
            if self.timed_out
            else EXIT_HEAD.format(code=self.exit_code)
        )
        return f"{head}\n{text}".rstrip()

    def failure_summary(self, limit: int, timeout_s: float = 0.0) -> str:
        failed = [] if self.timed_out else failed_tests(self.output)
        if not failed:
            return self.summary(limit, timeout_s)
        listing = "\n".join([FAILED_LIST_HEAD, *failed])
        tail = clip_tail(self.output, max(limit - len(listing), limit // 2))
        head = EXIT_HEAD.format(code=self.exit_code)
        return f"{head}\n{listing}\n{FAILED_TAIL_HEAD}\n{tail}".rstrip()


@dataclass(frozen=True)
class SessionReply:
    data: bytes | None
    timed_out: bool = False
    detail: str = ""


class PipeSession:
    def __init__(self, argv: Sequence[str], cwd: str | None = None) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None

    async def request(
        self,
        payload: bytes,
        timeout_s: float,
        spent: Callable[[], float] | None = None,
        on_call: CallHandler | None = None,
    ) -> SessionReply:
        if spent is None:
            started = time.monotonic()

            def spent() -> float:
                return time.monotonic() - started

        held = 0.0
        try:
            proc = await self._ensure()
        except OSError as exc:
            return SessionReply(None, detail=f"{type(exc).__name__}: {exc}")
        if not await self._send(proc, payload):
            return await self._died(proc)
        while True:
            read = asyncio.ensure_future(self._read_frame(proc))
            try:
                while not read.done():
                    left = timeout_s + held - spent()
                    if left <= 0:
                        await self.kill()
                        return SessionReply(None, timed_out=True)
                    await asyncio.wait({read}, timeout=min(SESSION_TICK_S, left))
            except BaseException:
                self._kill_group()
                raise
            finally:
                if not read.done():
                    read.cancel()
            data = read.result()
            if data is None:
                return await self._died(proc)
            call = call_request(data)
            if call is None:
                return SessionReply(data)
            began = time.monotonic()
            try:
                answer = await self._answer(on_call, call)
            except BaseException:
                self._kill_group()
                raise
            held += time.monotonic() - began
            if not await self._send(proc, json.dumps(answer).encode()):
                return await self._died(proc)

    @staticmethod
    async def _answer(on_call: CallHandler | None, call: dict[str, Any]) -> dict[str, Any]:
        if on_call is None:
            return {"ok": False, "reason": CALL_UNAVAILABLE}
        try:
            return await on_call(call)
        except Exception as exc:
            return {"ok": False, "reason": CALL_FAILED.format(error=f"{type(exc).__name__}: {exc}")}

    @staticmethod
    async def _send(proc: asyncio.subprocess.Process, data: bytes) -> bool:
        assert proc.stdin is not None
        try:
            proc.stdin.write(frame(data))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self._cwd,
                start_new_session=True,
            )
        return self._proc

    @staticmethod
    async def _read_frame(proc: asyncio.subprocess.Process) -> bytes | None:
        assert proc.stdout is not None
        try:
            head = await proc.stdout.readline()
            if not head:
                return None
            return await proc.stdout.readexactly(int(head))
        except (asyncio.IncompleteReadError, ValueError, ConnectionResetError):
            return None

    async def _died(self, proc: asyncio.subprocess.Process) -> SessionReply:
        try:
            await asyncio.wait_for(proc.wait(), SESSION_CLOSE_GRACE_S)
        except TimeoutError:
            await self.kill()
            return SessionReply(None, detail="процесс не отвечает")
        self._proc = None
        return SessionReply(None, detail=f"код {proc.returncode}")

    def _kill_group(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                os.killpg(self._proc.pid, signal.SIGKILL)
            except OSError:
                self._proc.kill()

    async def kill(self) -> None:
        proc = self._proc
        self._kill_group()
        self._proc = None
        if proc is not None:
            await proc.wait()

    async def close(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            return
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), SESSION_CLOSE_GRACE_S)
        except TimeoutError:
            await self.kill()
        self._proc = None


class Sandbox(Protocol):
    async def read_bytes(self, path: str) -> bytes | None: ...

    async def write_bytes(self, path: str, data: bytes) -> None: ...

    async def list_files(self) -> list[str]: ...

    async def exec(self, argv: Sequence[str], timeout_s: float) -> ExecResult: ...

    async def session_request(
        self,
        argv: Sequence[str],
        payload: bytes,
        timeout_s: float,
        on_call: CallHandler | None = None,
    ) -> SessionReply: ...

    async def session_close(self) -> None: ...

    async def diff(self, prefixes: tuple[str, ...] = ()) -> str: ...

    async def base_bytes(self, path: str) -> bytes | None: ...

    async def changed_paths(self) -> list[str]: ...

    async def changed_digests(self) -> dict[str, str]: ...

    async def exec_on_base(self, argv: Sequence[str], timeout_s: float) -> ExecResult | None: ...


class ManagedSandbox(Sandbox, Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def resume(self) -> None: ...

    async def aclose(self) -> None: ...


def safe_relative(path: str) -> str:
    pure = PurePosixPath(path.strip())
    if not path.strip() or pure.is_absolute() or ".." in pure.parts:
        raise SandboxPathError(f"путь {path!r} недопустим: нужен относительный путь внутри проекта")
    return str(pure)


class LocalSandbox:
    def __init__(
        self, root: Path, baseline: dict[str, str] | None = None, base_root: Path | None = None
    ) -> None:
        self._root = root
        self._base_root = base_root
        self._baseline = baseline if baseline is not None else self._snapshot()
        self._session: PipeSession | None = None

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

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self.session_close()

    async def resume(self) -> None:
        return None

    async def aclose(self) -> None:
        await self.session_close()

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

    async def session_request(
        self,
        argv: Sequence[str],
        payload: bytes,
        timeout_s: float,
        on_call: CallHandler | None = None,
    ) -> SessionReply:
        if self._session is None:
            self._session = PipeSession(argv, cwd=str(self._root))
        return await self._session.request(payload, timeout_s, on_call=on_call)

    async def session_close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()

    async def changed_digests(self) -> dict[str, str]:
        current = self._snapshot()
        changed = {rel: d for rel, d in current.items() if self._baseline.get(rel) != d}
        removed = {rel: "" for rel in self._baseline.keys() - current.keys()}
        return {**changed, **removed}

    async def exec_on_base(self, argv: Sequence[str], timeout_s: float) -> ExecResult | None:
        if self._base_root is None:
            return None
        try:
            result = await run(
                list(argv),
                timeout=timeout_s,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._base_root),
            )
        except TimeoutError:
            return ExecResult(TIMEOUT_EXIT_CODE, "", timed_out=True)
        except OSError as exc:
            return ExecResult(NOT_FOUND_EXIT_CODE, f"{type(exc).__name__}: {exc}")
        return ExecResult(result.returncode, result.stdout_text)

    async def base_bytes(self, path: str) -> bytes | None:
        if self._base_root is None:
            return None
        target = self._base_root / safe_relative(path)
        return target.read_bytes() if target.is_file() else None

    async def changed_paths(self) -> list[str]:
        return sorted(await self.changed_digests())

    def _text_of(self, root: Path | None, relative: str) -> list[str] | None:
        if root is None:
            return None
        target = root / relative
        if not target.is_file():
            return None
        try:
            return target.read_text().splitlines()
        except UnicodeDecodeError:
            return [BINARY_MARK]

    def _render_diff(self, paths: list[str]) -> str:
        parts = []
        for relative in paths:
            old = self._text_of(self._base_root, relative)
            new = self._text_of(self._root, relative)
            if old == new:
                continue
            parts.extend(
                difflib.unified_diff(
                    old or [],
                    new or [],
                    fromfile="/dev/null" if old is None else f"a/{relative}",
                    tofile="/dev/null" if new is None else f"b/{relative}",
                    lineterm="",
                )
            )
        return "\n".join(parts)

    async def diff(self, prefixes: tuple[str, ...] = ()) -> str:
        paths = await self.changed_paths()
        if prefixes:
            paths = [p for p in paths if p.startswith(prefixes)]
        return await asyncio.to_thread(self._render_diff, paths)
