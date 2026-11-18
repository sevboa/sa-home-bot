from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.llm.strata_switch import MAIN_DEMAND_FIELD, SILENCE_FIELD, SILENCE_PATH

log = logging.getLogger(__name__)

SILENCE_RELAY_TIMEOUT_S = 10.0
_NOT_SENT = object()


class SilenceRelay:
    def __init__(self, url: str, timeout_s: float = SILENCE_RELAY_TIMEOUT_S) -> None:
        self._url = url
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._sent: Any = _NOT_SENT
        self._ok = True

    async def push(self, reply: Mapping[str, Any]) -> None:
        if protocol.FIELD_SILENCE not in reply:
            return
        value = reply[protocol.FIELD_SILENCE]
        demand = reply.get(protocol.FIELD_LOCAL_DEMAND)
        if self._session is None:
            self._session = aiohttp.ClientSession()
        body = {SILENCE_FIELD: value}
        if protocol.FIELD_LOCAL_DEMAND in reply:
            body[MAIN_DEMAND_FIELD] = demand
        try:
            async with self._session.post(
                self._url.rstrip("/") + SILENCE_PATH,
                json=body,
                timeout=self._timeout,
            ) as resp:
                if resp.status == 200:
                    self._ok = True
                    if self._sent != (value, demand):
                        log.info(
                            "Тишина видеокарты передана strata-switch: %s, спрос %s", value, demand
                        )
                    self._sent = (value, demand)
                    return
                reason = f"HTTP {resp.status}"
        except (aiohttp.ClientError, TimeoutError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
        if self._ok:
            self._ok = False
            log.warning("strata-switch не принял тишину: %s", reason)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
