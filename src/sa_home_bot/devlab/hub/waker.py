from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sa_home_bot import wake_core
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.db.store import Store
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.store import HubStore

log = logging.getLogger(__name__)

WAKE_STEP_INTERVAL_S = 60.0
WAKE_BACKOFF_S = 600.0


class ExecutorWaker:
    def __init__(
        self,
        store: HubStore,
        node_link: ServiceLink,
        wake_store: Store,
        executor_node: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._node_link = node_link
        self._wake_store = wake_store
        self._executor_node = executor_node
        self._clock = clock
        self._last_attempt: float | None = None

    async def step(self) -> str | None:
        if not await self._store.queue.pending_work():
            return None
        if await self._store.executor_alive():
            return None
        now = self._clock()
        if self._last_attempt is not None and now - self._last_attempt < WAKE_BACKOFF_S:
            return None
        self._last_attempt = now
        outcome = await wake_core.ensure_service_ready(
            self._node_link, self._wake_store, self._executor_node, protocol.EXECUTOR_SERVICE
        )
        log.info("Будим исполнителя «%s»: %s", self._executor_node, outcome)
        return outcome
