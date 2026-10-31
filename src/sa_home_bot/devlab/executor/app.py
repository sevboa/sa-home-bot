from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from sa_home_bot.config import DevlabConfig, Settings
from sa_home_bot.devlab.executor.hub import ExecutorHub
from sa_home_bot.devlab.executor.podman import PodmanTestRunner
from sa_home_bot.devlab.executor.runner import TestRunner
from sa_home_bot.devlab.executor.worker import TestWorker, WorkerSettings
from sa_home_bot.utils.lifespan import Lifespan

log = logging.getLogger(__name__)

EXECUTOR_DIR = "executor"


def build_runner(cfg: DevlabConfig) -> TestRunner:
    return PodmanTestRunner(cfg)


def work_dir_of(cfg: DevlabConfig) -> Path:
    return Path(cfg.work_dir) if cfg.work_dir else Path(cfg.root) / EXECUTOR_DIR


async def run_executor(settings: Settings, runner: TestRunner | None = None) -> None:
    cfg = settings.devlab
    if not cfg.hub_url or not cfg.hub_token:
        raise RuntimeError("devlab: нужны hub_url и hub_token (роль executor)")
    runner = runner or build_runner(cfg)
    work_dir = work_dir_of(cfg)
    work_dir.mkdir(parents=True, exist_ok=True)
    hub = ExecutorHub(cfg.hub_url, cfg.hub_token)
    worker = TestWorker(
        hub,
        runner,
        WorkerSettings(
            hub_url=cfg.hub_url,
            token=cfg.hub_token,
            work_dir=work_dir,
            poll_interval_s=cfg.poll_interval_s,
            heartbeat_interval_s=cfg.heartbeat_interval_s,
        ),
    )
    lifespan = Lifespan()
    lifespan.install_signal_handlers()
    log.info("Служба devlab (исполнитель) запущена: хаб %s, каталог %s", hub.url, work_dir)
    task = asyncio.create_task(worker.run())
    try:
        await lifespan.wait()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        log.info("Останов службы devlab...")
        await hub.close()
        log.info("Служба devlab остановлена чисто")
