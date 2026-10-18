from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class Dedupable(Protocol):
    @property
    def dedup_key(self) -> str: ...


class DedupQueue:
    def __init__(self) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._keys: set[str] = set()

    async def put(self, job: Dedupable) -> bool:
        if job.dedup_key in self._keys:
            return False
        self._keys.add(job.dedup_key)
        await self._queue.put(job)
        return True

    async def get(self):
        job = await self._queue.get()
        if job is not None:
            self._keys.discard(job.dedup_key)
        return job

    def task_done(self) -> None:
        self._queue.task_done()

    async def stop(self) -> None:
        await self._queue.put(None)

    def qsize(self) -> int:
        return self._queue.qsize()
