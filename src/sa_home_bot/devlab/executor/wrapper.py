from __future__ import annotations

import asyncio
import io
import logging
import math
import re
import shlex
import tarfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.executor.runner import RunResult
from sa_home_bot.node.fixups.devlab import DEVLAB_WRAPPER_PATH
from sa_home_bot.utils import process
from sa_home_bot.utils.process import ProcResult

log = logging.getLogger(__name__)

Spawn = Callable[..., Awaitable[ProcResult]]
PackFilter = Callable[[str], bool]

EXEC_GRACE_S = 30.0
CLEANUP_TIMEOUT_S = 60.0
SYNC_TIMEOUT_S = 600.0
MEMBER_MAX_BYTES = 32 * 1024 * 1024
OUTPUT_TAIL_BYTES = 8 * 1024 * 1024
TIMEOUT_EXIT_CODE = 124
FAILURE_TEXT_CHARS = 2000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RunnerError(Exception):
    pass


def job_name(job_id: str) -> str:
    name = job_id.lower()
    if not NAME_RE.match(name):
        raise RunnerError(f"недопустимый id задания: {job_id!r}")
    return name


def archive_path(member_name: str) -> str:
    return "" if member_name == "." else member_name.removeprefix("./")


def pack_directory(directory: Path, skip: PackFilter | None = None) -> bytes:
    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        relative = archive_path(info.name)
        return None if skip is not None and relative and skip(relative) else info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(directory, arcname=".", filter=keep)
    return buffer.getvalue()


def extract_files(archive: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            for member in tar:
                if not member.isfile() or member.size > MEMBER_MAX_BYTES:
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    files[archive_path(member.name)] = handle.read()
    except tarfile.TarError:
        return {}
    return files


def extract_member(archive: bytes, name: str) -> bytes | None:
    return extract_files(archive).get(name)


class WrapperClient:
    def __init__(self, cfg: DevlabConfig, spawn: Spawn = process.run) -> None:
        self._spawn = spawn
        self._prefix = ["sudo", "-n", "-u", cfg.user, str(DEVLAB_WRAPPER_PATH)]

    async def call(
        self,
        *args: str,
        input: bytes | None = None,
        timeout: float | None = None,
        merge_stderr: bool = False,
    ) -> ProcResult:
        extra = {"stderr": asyncio.subprocess.STDOUT} if merge_stderr else {}
        return await self._spawn([*self._prefix, *args], input=input, timeout=timeout, **extra)

    async def checked(self, what: str, *args: str, **kwargs) -> ProcResult:
        result = await self.call(*args, merge_stderr=True, **kwargs)
        if result.returncode != 0:
            text = result.stdout_text[-FAILURE_TEXT_CHARS:].strip()
            raise RunnerError(f"{what}: код {result.returncode}\n{text}")
        return result

    async def push_tree(
        self, command: str, name: str, directory: Path, skip: PackFilter | None = None
    ) -> None:
        archive = await asyncio.to_thread(pack_directory, directory, skip)
        await self.checked(command, command, name, input=archive, timeout=SYNC_TIMEOUT_S)

    async def exec(self, name: str, argv: Sequence[str], timeout_s: float) -> RunResult:
        limit = max(1, math.ceil(timeout_s))
        try:
            result = await asyncio.wait_for(
                self.call("exec", name, str(limit), shlex.join(argv), merge_stderr=True),
                timeout=limit + EXEC_GRACE_S,
            )
        except TimeoutError:
            return RunResult(exit_code=TIMEOUT_EXIT_CODE, output=b"", timed_out=True)
        return RunResult(
            exit_code=result.returncode,
            output=result.stdout[-OUTPUT_TAIL_BYTES:],
            timed_out=result.returncode == TIMEOUT_EXIT_CODE,
        )

    async def create(self, name: str, tag: str, cpus: float, memory: str, cpuset: str) -> None:
        await self.remove_container(name)
        await self.checked(
            "создание контейнера",
            "create",
            name,
            tag,
            f"{cpus:g}",
            memory,
            cpuset,
            timeout=SYNC_TIMEOUT_S,
        )

    async def remove_container(self, name: str) -> None:
        try:
            await self.call("rm", name, merge_stderr=True, timeout=CLEANUP_TIMEOUT_S)
        except Exception as exc:
            log.warning("rm %s: %s", name, exc)

    async def cleanup(self, name: str) -> None:
        await self.remove_container(name)
        try:
            await self.call("clean", name, merge_stderr=True, timeout=CLEANUP_TIMEOUT_S)
        except Exception as exc:
            log.warning("clean %s: %s", name, exc)
