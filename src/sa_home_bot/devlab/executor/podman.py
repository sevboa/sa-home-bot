from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import re
import shlex
import tarfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.executor.runner import JUNIT_FILE, RunRequest, RunResult
from sa_home_bot.node.fixups.devlab import DEVLAB_WRAPPER_PATH
from sa_home_bot.utils import process
from sa_home_bot.utils.process import ProcResult

log = logging.getLogger(__name__)

Spawn = Callable[..., Awaitable[ProcResult]]

CONTAINERFILE_VERSION = 1
IMAGE_TAG_PREFIX = "t"
IMAGE_TAG_HASH_CHARS = 24
SETUP_DIR = "/setup"
EXEC_GRACE_S = 30.0
CLEANUP_TIMEOUT_S = 60.0
SYNC_TIMEOUT_S = 600.0
JUNIT_MAX_BYTES = 32 * 1024 * 1024
OUTPUT_TAIL_BYTES = 8 * 1024 * 1024
TIMEOUT_EXIT_CODE = 124
CANCELLED_EXIT_CODE = 130
FAILURE_TEXT_CHARS = 2000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RunnerError(Exception):
    pass


def job_name(job_id: str) -> str:
    name = job_id.lower()
    if not NAME_RE.match(name):
        raise RunnerError(f"недопустимый id задания: {job_id!r}")
    return name


def read_setup_files(workdir: Path, names: Sequence[str]) -> dict[str, bytes]:
    root = workdir.resolve()
    files: dict[str, bytes] = {}
    for name in names:
        rel = PurePosixPath(name)
        if not name or rel.is_absolute() or ".." in rel.parts or "\n" in name:
            raise RunnerError(f"setup_files: недопустимый путь {name!r}")
        path = (root / rel).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise RunnerError(f"setup_files: нет файла {name!r} в коде задания")
        files[rel.as_posix()] = path.read_bytes()
    return files


def check_image(image: str) -> str:
    if not IMAGE_RE.match(image):
        raise RunnerError(f"недопустимое имя образа: {image!r}")
    return image


def check_env(env: Sequence[tuple[str, str]]) -> None:
    for key, _ in env:
        if not ENV_KEY_RE.match(key):
            raise RunnerError(f"недопустимое имя переменной окружения: {key!r}")


def containerfile(
    image: str, setup: Sequence[str], files: dict[str, bytes], env: Sequence[tuple[str, str]]
) -> str:
    lines = [f"FROM {check_image(image)}", f"WORKDIR {SETUP_DIR}"]
    check_env(env)
    lines += [f"ENV {key}={json.dumps(value)}" for key, value in env]
    for name in sorted(files):
        lines.append(f"COPY {json.dumps([name, f'{SETUP_DIR}/{name}'])}")
    lines += [f"RUN {json.dumps(['sh', '-c', command])}" for command in setup]
    lines.append("WORKDIR /work")
    return "\n".join(lines) + "\n"


def image_tag(
    image: str, setup: Sequence[str], files: dict[str, bytes], env: Sequence[tuple[str, str]]
) -> str:
    digest = hashlib.sha256()
    payload = {
        "version": CONTAINERFILE_VERSION,
        "containerfile": containerfile(image, setup, files, env),
        "files": {name: hashlib.sha256(files[name]).hexdigest() for name in sorted(files)},
    }
    digest.update(json.dumps(payload, sort_keys=True).encode())
    return IMAGE_TAG_PREFIX + digest.hexdigest()[:IMAGE_TAG_HASH_CHARS]


def pack_directory(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(directory, arcname=".")
    return buffer.getvalue()


def extract_member(archive: bytes, name: str) -> bytes | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
            for member in tar:
                if member.isfile() and member.name.removeprefix("./") == name:
                    if member.size > JUNIT_MAX_BYTES:
                        return None
                    handle = tar.extractfile(member)
                    return handle.read() if handle else None
    except tarfile.TarError:
        return None
    return None


def image_names(listing: str) -> set[str]:
    try:
        entries = json.loads(listing or "[]") or []
    except json.JSONDecodeError:
        return set()
    names: set[str] = set()
    for entry in entries:
        for raw in (entry.get("Names") or []) if isinstance(entry, dict) else []:
            names.add(raw.rsplit(":", 1)[0].rsplit("/", 1)[-1])
    return names


@dataclass
class _Active:
    name: str
    task: asyncio.Task[RunResult] | None = None
    cancelled: bool = False


class PodmanTestRunner:
    def __init__(self, cfg: DevlabConfig, spawn: Spawn = process.run) -> None:
        self._cfg = cfg
        self._spawn = spawn
        self._active: dict[str, _Active] = {}
        self._prefix = ["sudo", "-n", "-u", cfg.user, str(DEVLAB_WRAPPER_PATH)]

    async def _wrapper(
        self,
        *args: str,
        input: bytes | None = None,
        timeout: float | None = None,
        merge_stderr: bool = False,
    ) -> ProcResult:
        extra = {"stderr": asyncio.subprocess.STDOUT} if merge_stderr else {}
        return await self._spawn([*self._prefix, *args], input=input, timeout=timeout, **extra)

    async def _checked(self, what: str, *args: str, **kwargs) -> ProcResult:
        result = await self._wrapper(*args, merge_stderr=True, **kwargs)
        if result.returncode != 0:
            text = result.stdout_text[-FAILURE_TEXT_CHARS:].strip()
            raise RunnerError(f"{what}: код {result.returncode}\n{text}")
        return result

    async def run(self, request: RunRequest) -> RunResult:
        name = job_name(request.job_id)
        if request.job_id in self._active:
            raise RunnerError(f"задание {request.job_id} уже выполняется")
        active = _Active(name=name)
        self._active[request.job_id] = active
        active.task = asyncio.ensure_future(self._pipeline(request, active))
        try:
            return await active.task
        except asyncio.CancelledError:
            if active.cancelled and not asyncio.current_task().cancelling():
                return RunResult(exit_code=CANCELLED_EXIT_CODE, output=b"")
            raise
        finally:
            self._active.pop(request.job_id, None)

    async def cancel(self, job_id: str) -> None:
        active = self._active.get(job_id)
        if active is None:
            return
        active.cancelled = True
        await self._remove_container(active.name)
        if active.task is not None:
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)

    async def _pipeline(self, request: RunRequest, active: _Active) -> RunResult:
        name = active.name
        try:
            await self._sync_in(name, request.workdir)
            tag = await self._ensure_image(name, request)
            await self._create(name, tag)
            result = await self._exec(name, request)
            if active.cancelled:
                return RunResult(exit_code=CANCELLED_EXIT_CODE, output=b"")
            if result.timed_out:
                await self._remove_container(name)
            await self._sync_out(name, request.workdir)
            return result
        finally:
            await asyncio.shield(self._cleanup(name))

    async def _sync_in(self, name: str, workdir: Path) -> None:
        archive = await asyncio.to_thread(pack_directory, workdir)
        await self._checked("sync-in", "sync-in", name, input=archive, timeout=SYNC_TIMEOUT_S)

    async def _ensure_image(self, name: str, request: RunRequest) -> str:
        image = request.image or self._cfg.test_image
        files = read_setup_files(request.workdir, request.setup_files)
        tag = image_tag(image, request.setup, files, request.env)
        listing = await self._wrapper("images", timeout=SYNC_TIMEOUT_S)
        if listing.returncode == 0 and tag in image_names(listing.stdout_text):
            return tag
        text = containerfile(image, request.setup, files, request.env)
        log.info("Сборка образа devlab/%s для задания %s", tag, request.job_id)
        await self._checked(
            "сборка образа",
            "build",
            name,
            tag,
            input=text.encode(),
            timeout=self._cfg.build_timeout_s,
        )
        return tag

    async def _create(self, name: str, tag: str) -> None:
        await self._remove_container(name)
        await self._checked(
            "создание контейнера",
            "create",
            name,
            tag,
            f"{self._cfg.test_cpus:g}",
            self._cfg.test_memory,
            self._cfg.test_cpuset,
            timeout=SYNC_TIMEOUT_S,
        )

    async def _exec(self, name: str, request: RunRequest) -> RunResult:
        limit = max(1, math.ceil(request.timeout_s))
        command = shlex.join(request.argv)
        try:
            result = await asyncio.wait_for(
                self._wrapper("exec", name, str(limit), command, merge_stderr=True),
                timeout=limit + EXEC_GRACE_S,
            )
        except TimeoutError:
            return RunResult(exit_code=TIMEOUT_EXIT_CODE, output=b"", timed_out=True)
        output = result.stdout[-OUTPUT_TAIL_BYTES:]
        timed_out = result.returncode == TIMEOUT_EXIT_CODE
        return RunResult(exit_code=result.returncode, output=output, timed_out=timed_out)

    async def _sync_out(self, name: str, workdir: Path) -> None:
        try:
            result = await self._wrapper("sync-out", name, JUNIT_FILE, timeout=SYNC_TIMEOUT_S)
        except Exception as exc:
            log.warning("sync-out %s: %s", name, exc)
            return
        if result.returncode != 0:
            return
        junit = extract_member(result.stdout, JUNIT_FILE)
        if junit is not None:
            (workdir / JUNIT_FILE).write_bytes(junit)

    async def _remove_container(self, name: str) -> None:
        try:
            await self._wrapper("rm", name, merge_stderr=True, timeout=CLEANUP_TIMEOUT_S)
        except Exception as exc:
            log.warning("rm %s: %s", name, exc)

    async def _cleanup(self, name: str) -> None:
        await self._remove_container(name)
        try:
            await self._wrapper("clean", name, merge_stderr=True, timeout=CLEANUP_TIMEOUT_S)
        except Exception as exc:
            log.warning("clean %s: %s", name, exc)
