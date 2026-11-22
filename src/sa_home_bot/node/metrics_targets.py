from __future__ import annotations

import asyncio
import logging

from sa_home_bot.bot.vpn_nodes import known_server_nodes
from sa_home_bot.config import Settings
from sa_home_bot.devlab.connect import local_tailscale_address
from sa_home_bot.metrics.runtime import runtime_files, write_changed
from sa_home_bot.node.state import NodeState
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

REFRESH_INTERVAL_S = 60.0


class MetricsTargetsWriter:
    def __init__(
        self, settings: Settings, state: NodeState, *, interval_s: float = REFRESH_INTERVAL_S
    ) -> None:
        self._settings = settings
        self._state = state
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="metrics-targets")

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None

    async def refresh(self) -> list:
        own_host = await asyncio.to_thread(local_tailscale_address)
        known = await asyncio.to_thread(known_server_nodes)
        files = runtime_files(self._settings, own_host, known, list(self._state.peers))
        return await asyncio.to_thread(write_changed, files)

    async def _run(self) -> None:
        while True:
            try:
                written = await self.refresh()
                if written:
                    log.info("Метрики: обновлены файлы %s", ", ".join(str(p) for p in written))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Метрики: не удалось обновить цели и дашборды", exc_info=True)
            await asyncio.sleep(self._interval_s)
