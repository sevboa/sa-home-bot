from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Collection
from pathlib import Path

from sa_home_bot.devlab.executor.agent_worker import AGENT_DIR, AGENT_STATE_FILE
from sa_home_bot.devlab.executor.worker import JOBS_DIR
from sa_home_bot.devlab.executor.wrapper import WrapperClient

log = logging.getLogger(__name__)


def remove_entry(entry: Path) -> bool:
    try:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Не удалось убрать %s: %s", entry, exc)
        return False
    return True


def swept_roots(work_dir: Path) -> list[Path]:
    roots = [work_dir / JOBS_DIR]
    agent_root = work_dir / AGENT_DIR
    if (work_dir / AGENT_STATE_FILE).is_file() or not any_entry(agent_root):
        roots.append(agent_root)
    else:
        log.warning("Нет %s при непустом %s: каталог агента не чищу", AGENT_STATE_FILE, agent_root)
    return roots


def any_entry(root: Path) -> bool:
    return root.is_dir() and any(root.iterdir())


def remove_foreign_entries(work_dir: Path, keep: Collection[str]) -> int:
    removed = 0
    for root in swept_roots(work_dir):
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.name not in keep and remove_entry(entry):
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
