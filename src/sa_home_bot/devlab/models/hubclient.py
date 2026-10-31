from __future__ import annotations

import time
from collections.abc import Callable

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.store import HEARTBEAT_FRESH_S, Principal, hash_token
from sa_home_bot.devlab.hubhttp import HubHttp

HEARTBEAT_INTERVAL_S = HEARTBEAT_FRESH_S / 4
INTROSPECT_TTL_S = 120.0


class HubUnavailableError(Exception):
    pass


class HubClient(HubHttp):
    def __init__(
        self,
        hub_url: str,
        token: str,
        session: aiohttp.ClientSession | None = None,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = INTROSPECT_TTL_S,
    ) -> None:
        super().__init__(hub_url, token, session)
        self._clock = clock
        self._ttl_s = ttl_s
        self._cache: dict[tuple[str, str], tuple[float, Principal | None]] = {}

    async def introspect(self, token: str, address: str) -> Principal | None:
        key = (hash_token(token), address)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with self._session.post(
                self._url + protocol.HTTP_INTROSPECT_PATH,
                json={protocol.FIELD_TOKEN: token, protocol.FIELD_ADDRESS: address},
                headers=self._headers,
            ) as resp:
                if resp.status == 404:
                    principal = None
                elif resp.status == 200:
                    data = await resp.json()
                    principal = Principal(data[protocol.FIELD_NAME], data[protocol.FIELD_ROLE])
                else:
                    raise HubUnavailableError(f"хаб ответил HTTP {resp.status}")
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise HubUnavailableError(type(exc).__name__) from exc
        self._cache[key] = (now + self._ttl_s, principal)
        return principal
