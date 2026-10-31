from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from sa_home_bot.proto.endpoints import TcpEndpoint, parse_endpoint

log = logging.getLogger(__name__)

NODES_TTL_S = 30.0
RESOLVE_TIMEOUT_S = 3.0

Addresses = dict[str, frozenset[str]]


class NodeDirectory(Protocol):
    async def addresses(self) -> Addresses: ...


async def resolve_ipv4(host: str) -> frozenset[str]:
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(
            host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        ),
        timeout=RESOLVE_TIMEOUT_S,
    )
    return frozenset(info[4][0] for info in infos)


def node_at(addresses: Addresses, ip: str | None) -> str | None:
    owners = [name for name, ips in addresses.items() if ip in ips]
    return owners[0] if len(owners) == 1 else None


class SwarmNodes:
    def __init__(
        self,
        fetch_state: Callable[[], Awaitable[dict[str, Any]]],
        own_name: str,
        own_ip: str,
        clock: Callable[[], float] = time.monotonic,
        resolve: Callable[[str], Awaitable[frozenset[str]]] = resolve_ipv4,
        ttl_s: float = NODES_TTL_S,
    ) -> None:
        self._fetch_state = fetch_state
        self._resolve = resolve
        self._clock = clock
        self._ttl_s = ttl_s
        self._known: Addresses = {own_name: frozenset({own_ip})}
        self._own = (own_name, frozenset({own_ip}))
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def addresses(self) -> Addresses:
        async with self._lock:
            if self._clock() >= self._expires:
                await self._refresh()
            return dict(self._known)

    async def _refresh(self) -> None:
        self._expires = self._clock() + self._ttl_s
        try:
            state = await self._fetch_state()
            fresh = await self._from_state(state)
        except Exception as exc:
            log.warning("Список нод роя не обновился (%s), беру прежний", type(exc).__name__)
            return
        self._known = fresh

    async def _from_state(self, state: dict[str, Any]) -> Addresses:
        own_name, own_ips = self._own
        fresh: Addresses = {own_name: own_ips}
        for peer in state.get("peers") or []:
            name = peer.get("id")
            if not isinstance(name, str) or not name or name == own_name:
                continue
            ips = await self._peer_ips(peer)
            fresh[name] = ips or self._known.get(name, frozenset())
        return fresh

    async def _peer_ips(self, peer: dict[str, Any]) -> frozenset[str]:
        ips: set[str] = set()
        for raw in peer.get("endpoints") or [peer.get("endpoint")]:
            try:
                endpoint = parse_endpoint(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(endpoint, TcpEndpoint):
                continue
            try:
                ips |= await self._resolve(endpoint.host)
            except (OSError, TimeoutError):
                log.warning("Адрес %s ноды %s не разрешился", endpoint.host, peer.get("id"))
        return frozenset(ips)
