from __future__ import annotations

import os
import shutil
from collections.abc import AsyncIterable
from pathlib import Path

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError

PARTIAL_SUFFIX = ".part"


class LogTooLarge(HubError):
    pass


def job_dir(root: Path, job_id: str) -> Path:
    return root / job_id


def check_name(name: str) -> str:
    if name not in protocol.LOG_FILES:
        raise HubError(f"файл лога: {', '.join(protocol.LOG_FILES)}")
    return name


async def save(
    root: Path,
    job_id: str,
    name: str,
    chunks: AsyncIterable[bytes],
    limit: int | None = None,
) -> int:
    directory = job_dir(root, job_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / check_name(name)
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    limit = protocol.LOG_MAX_BYTES if limit is None else limit
    size = 0
    try:
        with partial.open("wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise LogTooLarge(f"лог больше {limit} байт")
                out.write(chunk)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return size


def read_tail(root: Path, job_id: str, name: str, limit: int) -> tuple[str, int] | None:
    path = job_dir(root, job_id) / check_name(name)
    if not path.is_file():
        return None
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - limit))
        data = handle.read()
    return data.decode("utf-8", errors="replace"), size


def remove(root: Path, job_id: str) -> None:
    shutil.rmtree(job_dir(root, job_id), ignore_errors=True)
