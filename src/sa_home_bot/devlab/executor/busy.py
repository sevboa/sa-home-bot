from __future__ import annotations

import time
from collections.abc import Callable

DEFAULT_OWNER_WAIT_S = 3600.0


def no_outbox() -> int:
    return 0


class BusyTracker:
    def __init__(
        self,
        owner_wait_s: float = DEFAULT_OWNER_WAIT_S,
        clock: Callable[[], float] = time.time,
        outbox_size: Callable[[], int] = no_outbox,
    ) -> None:
        self._owner_wait_s = owner_wait_s
        self._clock = clock
        self.outbox_size = outbox_size
        self._running: set[str] = set()
        self._waiting: dict[str, float] = {}
        self._queued = 0

    def restore(self, waiting: dict[str, float]) -> None:
        self._waiting = dict(waiting)

    def waiting_since(self) -> dict[str, float]:
        return dict(self._waiting)

    def running_jobs(self) -> list[str]:
        return sorted(self._running)

    def started(self, job_id: str) -> None:
        self._waiting.pop(job_id, None)
        self._running.add(job_id)

    def waiting_owner(self, job_id: str) -> None:
        self._running.discard(job_id)
        self._waiting[job_id] = self._clock()

    def released(self, job_id: str) -> None:
        self._waiting.pop(job_id, None)

    def finished(self, job_id: str) -> None:
        self._running.discard(job_id)
        self._waiting.pop(job_id, None)

    def set_queued(self, count: int) -> None:
        self._queued = count

    def holding_for_owner(self) -> list[str]:
        now = self._clock()
        return sorted(
            job_id for job_id, since in self._waiting.items() if now - since < self._owner_wait_s
        )

    def busy(self) -> bool:
        return bool(
            self._running or self._queued > 0 or self.holding_for_owner() or self.outbox_size() > 0
        )
