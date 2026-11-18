from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.llm.strata_switch import SILENCE_FIELD, SILENCE_PATH

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
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.post(
                self._url.rstrip("/") + SILENCE_PATH,
                json={SILENCE_FIELD: value},
                timeout=self._timeout,
            ) as resp:
                if resp.status == 200:
                    if not self._ok:
                        self._ok = True
                    if self._sent == _NOT_SENT or self._sent != value:
                        log.info("Тишина видеокарты передана strata-switch: %s", value)
                    self._sent = value
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
