from __future__ import annotations

import logging
from typing import Any

import aiohttp

from sa_home_bot.devlab import protocol

log = logging.getLogger(__name__)

HUB_TIMEOUT = aiohttp.ClientTimeout(total=15)


class HubHttp:
    def __init__(
        self, hub_url: str, token: str, session: aiohttp.ClientSession | None = None
    ) -> None:
        self._url = hub_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._session = session or aiohttp.ClientSession(timeout=HUB_TIMEOUT)

    @property
    def url(self) -> str:
        return self._url

    async def close(self) -> None:
        await self._session.close()

    async def heartbeat(self, body: dict[str, Any]) -> None:
        try:
            async with self._session.post(
                self._url + protocol.HTTP_HEARTBEAT_PATH, json=body, headers=self._headers
            ) as resp:
                if resp.status != 200:
                    log.warning("Heartbeat в хаб отвергнут: HTTP %d", resp.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Heartbeat в хаб не прошёл: %s", type(exc).__name__)
