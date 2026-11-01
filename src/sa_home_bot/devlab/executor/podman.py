from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from sa_home_bot.config import DevlabConfig
from sa_home_bot.devlab.executor.image import ImageRecipe, ensure_image
from sa_home_bot.devlab.executor.runner import JUNIT_FILE, RunRequest, RunResult
from sa_home_bot.devlab.executor.wrapper import (
    SYNC_TIMEOUT_S,
    PauseClock,
    RunnerError,
    Spawn,
    WrapperClient,
    extract_member,
    job_name,
)
from sa_home_bot.utils import process

log = logging.getLogger(__name__)

CANCELLED_EXIT_CODE = 130


@dataclass
class _Active:
    name: str
    task: asyncio.Task[RunResult] | None = None
    cancelled: bool = False


class PodmanTestRunner:
    def __init__(
        self, cfg: DevlabConfig, spawn: Spawn = process.run, pause: PauseClock | None = None
    ) -> None:
        self._cfg = cfg
        self._wrapper = WrapperClient(cfg, spawn, pause)
        self._active: dict[str, _Active] = {}

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
        await self._wrapper.remove_container(active.name)
        if active.task is not None:
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)

    async def _pipeline(self, request: RunRequest, active: _Active) -> RunResult:
        name = active.name
        try:
            await self._wrapper.push_tree("sync-in", name, request.workdir)
            recipe = ImageRecipe(
                image=request.image or self._cfg.test_image,
                setup=request.setup,
                setup_files=request.setup_files,
                env=request.env,
            )
            tag = await ensure_image(
                self._wrapper, name, recipe, request.workdir, self._cfg.build_timeout_s
            )
            await self._wrapper.create(
                name, tag, self._cfg.test_cpus, self._cfg.test_memory, self._cfg.test_cpuset
            )
            result = await self._wrapper.exec(name, request.argv, request.timeout_s)
            if active.cancelled:
                return RunResult(exit_code=CANCELLED_EXIT_CODE, output=b"")
            if result.timed_out:
                await self._wrapper.remove_container(name)
            await self._sync_out(name, request.workdir)
            return result
        finally:
            await asyncio.shield(self._wrapper.cleanup(name))

    async def _sync_out(self, name: str, workdir: Path) -> None:
        try:
            result = await self._wrapper.call("sync-out", name, JUNIT_FILE, timeout=SYNC_TIMEOUT_S)
        except Exception as exc:
            log.warning("sync-out %s: %s", name, exc)
            return
        if result.returncode != 0:
            return
        junit = extract_member(result.stdout, JUNIT_FILE)
        if junit is not None:
            (workdir / JUNIT_FILE).write_bytes(junit)
