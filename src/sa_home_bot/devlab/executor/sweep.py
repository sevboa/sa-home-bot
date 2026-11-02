from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Collection
from pathlib import Path

from sa_home_bot.devlab.executor.agent_worker import AGENT_DIR
from sa_home_bot.devlab.executor.worker import JOBS_DIR
from sa_home_bot.devlab.executor.wrapper import WrapperClient

log = logging.getLogger(__name__)

SWEPT_DIRS = (JOBS_DIR, AGENT_DIR)


def remove_foreign_entries(work_dir: Path, keep: Collection[str]) -> int:
    removed = 0
    for sub in SWEPT_DIRS:
        root = work_dir / sub
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.name in keep:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
            removed += 1
    return removed


async def sweep_orphans(wrapper: WrapperClient, work_dir: Path, keep: Collection[str]) -> None:
    try:
        removed = await asyncio.to_thread(remove_foreign_entries, work_dir, keep)
        if removed:
            log.info("Убрано осиротевших рабочих копий: %d", removed)
    except OSError as exc:
        log.warning("Уборка рабочих копий: %s", exc)
    try:
        await wrapper.sweep(keep)
    except Exception as exc:
        log.warning("Уборка контейнеров и копий обёртки: %s", exc)
