from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.agent.paths import matches
from sa_home_bot.devlab.agent.sandbox import (
    IGNORED_DIRS,
    ExecResult,
    LocalSandbox,
    SandboxPathError,
    safe_relative,
)
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.executor.image import ImageRecipe, ensure_image
from sa_home_bot.devlab.executor.runner import JUNIT_FILE
from sa_home_bot.devlab.executor.wrapper import (
    SYNC_TIMEOUT_S,
    RunnerError,
    Spawn,
    WrapperClient,
    extract_files,
    job_name,
)
from sa_home_bot.utils import process

log = logging.getLogger(__name__)


class PodmanSandbox:
    def __init__(
        self,
        cfg: DevlabConfig,
        job_id: str,
        workdir: Path,
        recipe: ImageRecipe,
        spec: AgentSpec,
        baseline: dict[str, str] | None = None,
        spawn: Spawn = process.run,
    ) -> None:
        self._cfg = cfg
        self._name = job_name(job_id)
        self._workdir = workdir
        self._recipe = recipe
        self._spec = spec
        self._wrapper = WrapperClient(cfg, spawn)
        self._local = LocalSandbox(workdir, baseline)
        self._lock = asyncio.Lock()
        self._tag = ""
        self._running = False
        self._stale = True
        self._lost = False

    @property
    def name(self) -> str:
        return self._name

    def _is_secret(self, relative: str) -> bool:
        return matches(relative, self._spec.secret_paths)

    def _is_protected(self, relative: str) -> bool:
        return matches(relative, self._spec.forbidden_paths)

    def _is_foreign(self, relative: str) -> bool:
        return (
            relative == JUNIT_FILE
            or bool(IGNORED_DIRS.intersection(PurePosixPath(relative).parts))
            or self._is_secret(relative)
        )

    def _checked_path(self, path: str) -> str:
        relative = safe_relative(path)
        if self._is_secret(relative):
            raise SandboxPathError(f"путь {relative} закрыт правилами проекта")
        return relative

    async def read_bytes(self, path: str) -> bytes | None:
        return await self._local.read_bytes(self._checked_path(path))

    async def write_bytes(self, path: str, data: bytes) -> None:
        relative = self._checked_path(path)
        if self._is_protected(relative):
            raise SandboxPathError(f"путь {relative} закрыт для правок правилами проекта")
        await self._local.write_bytes(relative, data)
        self._stale = True

    async def list_files(self) -> list[str]:
        return [rel for rel in await self._local.list_files() if not self._is_secret(rel)]

    async def changed_paths(self) -> list[str]:
        return await self._local.changed_paths()

    async def start(self) -> None:
        async with self._lock:
            await self._provision()

    async def stop(self) -> None:
        async with self._lock:
            if self._running:
                await self._wrapper.call("stop", self._name, merge_stderr=True)
            self._running = False

    async def resume(self) -> None:
        async with self._lock:
            if not self._lost and self._tag:
                started = await self._wrapper.call("start", self._name, merge_stderr=True)
                if started.returncode == 0:
                    self._running = True
                    self._stale = True
                    return
            await self._provision()

    async def aclose(self) -> None:
        self._running = False
        await asyncio.shield(self._wrapper.cleanup(self._name))

    async def exec(self, argv: Sequence[str], timeout_s: float) -> ExecResult:
        async with self._lock:
            await self._ready()
            result = await self._wrapper.exec(self._name, argv, timeout_s)
            await self._pull()
            if result.timed_out:
                await self._wrapper.remove_container(self._name)
                self._lost = True
        return ExecResult(
            result.exit_code, result.output.decode(errors="replace"), timed_out=result.timed_out
        )

    async def _provision(self) -> None:
        await self._wrapper.push_tree("sync-in", self._name, self._workdir, self._is_foreign)
        self._tag = await ensure_image(
            self._wrapper, self._name, self._recipe, self._workdir, self._cfg.build_timeout_s
        )
        await self._create()
        self._stale = False

    async def _create(self) -> None:
        await self._wrapper.create(
            self._name,
            self._tag,
            self._cfg.agent_cpus,
            self._cfg.agent_memory,
            self._cfg.agent_cpuset,
        )
        self._running = True
        self._lost = False

    async def _ready(self) -> None:
        if not self._running:
            raise RunnerError(f"контейнер {self._name} не запущен")
        if self._lost:
            await self._create()
            self._stale = True
        if self._stale:
            await self._wrapper.push_tree("refresh", self._name, self._workdir, self._is_foreign)
            self._stale = False

    async def _pull(self) -> None:
        try:
            result = await self._wrapper.call("sync-out", self._name, timeout=SYNC_TIMEOUT_S)
        except Exception as exc:
            log.warning("sync-out %s: %s", self._name, exc)
            return
        if result.returncode != 0:
            log.warning("sync-out %s: код %s", self._name, result.returncode)
            return
        remote = await asyncio.to_thread(extract_files, result.stdout)
        await self._reconcile(remote)

    async def _reconcile(self, remote: dict[str, bytes]) -> None:
        seen: set[str] = set()
        for name, data in remote.items():
            try:
                relative = safe_relative(name)
            except SandboxPathError:
                continue
            if self._is_foreign(relative):
                continue
            seen.add(relative)
            current = await self._local.read_bytes(relative)
            if current == data:
                continue
            if self._is_protected(relative):
                self._stale = True
            else:
                await self._local.write_bytes(relative, data)
        for relative in await self._local.list_files():
            if relative in seen or self._is_foreign(relative):
                continue
            if self._is_protected(relative):
                self._stale = True
            else:
                (self._workdir / relative).unlink(missing_ok=True)
