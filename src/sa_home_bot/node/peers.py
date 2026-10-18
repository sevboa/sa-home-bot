from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from sa_home_bot.proto.addresses import NODE_SERVICE
from sa_home_bot.proto.client import EventCallback, ProtoClient
from sa_home_bot.proto.endpoints import (
    Endpoint,
    endpoint_rank,
    is_loopback,
    parse_endpoint,
)
from sa_home_bot.proto.messages import (
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Address,
    Envelope,
    ProtoError,
    ServiceInfo,
)

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0

CONNECT_TIMEOUT_S = 5.0

MAX_ENDPOINTS = 8

HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_TIMEOUT_S = 4.0
HEARTBEAT_MISSES = 2

LOCAL_HEARTBEAT_TIMEOUT_S = 20.0

NODE_INCARNATION = uuid.uuid4().hex


def _parse_all(values: Sequence[str | Path | Endpoint]) -> list[Endpoint]:
    out: list[Endpoint] = []
    for value in values:
        try:
            ep = parse_endpoint(value)
        except ValueError as exc:
            log.warning("PeerLink: пропускаю адрес %r (%s)", value, exc)
            continue
        if ep not in out:
            out.append(ep)
    return out


async def _handshake(client: ProtoClient) -> ServiceInfo:
    await client.connect()
    return await client.hello()


def _answers_to(info: ServiceInfo, name: str) -> bool:
    return name.lower() in (info.node.lower(), info.service.lower())


def _order_endpoints(
    endpoints: Sequence[Endpoint], first: Endpoint | None = None
) -> list[Endpoint]:
    ordered = sorted(endpoints, key=endpoint_rank)
    if first is not None and first in ordered:
        ordered.remove(first)
        ordered.insert(0, first)
    return ordered


class PeerLink:
    def __init__(
        self,
        name: str,
        endpoint: str | Endpoint | Sequence[str | Endpoint],
        *,
        token: str = "",
        on_event: EventCallback | None = None,
        on_endpoints: Callable[[str, list[str]], None] | None = None,
        reconnect_delay: float = RECONNECT_DELAY_S,
        self_node: str = "",
        heartbeat_timeout: float = HEARTBEAT_TIMEOUT_S,
    ) -> None:
        self.name = name
        self._heartbeat_timeout = heartbeat_timeout
        raw = [endpoint] if isinstance(endpoint, (str, Path, Endpoint)) else list(endpoint)
        parsed = _parse_all(raw)
        if not parsed:
            raise ValueError(f"линку {name} не задан ни один endpoint")
        self._candidates: list[Endpoint] = _order_endpoints(parsed, first=parsed[0])
        self.endpoint: Endpoint = self._candidates[0]
        self._on_endpoints = on_endpoints
        self._token = token
        self._on_event = on_event
        self._delay = reconnect_delay
        self._src = Address(node=self_node) if self_node else None
        self._self_node = self_node.lower() if self_node else ""
        self._client: ProtoClient | None = None
        self._task: asyncio.Task | None = None
        self._last_error: Exception | None = None
        self._dropping = False
        self._stopping = False
        self.node_kind: str = ""
        self.wake_info: dict[str, str] | None = None
        self.down_since: float | None = None
        self.left: bool = False

    @property
    def alive(self) -> bool:
        return self._client is not None

    @property
    def endpoints(self) -> list[str]:
        return [str(ep) for ep in self._candidates]

    def learn_endpoints(self, advertised: Sequence[str]) -> None:
        fresh: list[Endpoint] = [self.endpoint]
        seen: set[Endpoint] = {self.endpoint}
        for ep in _parse_all(list(advertised)):
            if ep in seen or is_loopback(ep) or len(fresh) >= MAX_ENDPOINTS:
                continue
            seen.add(ep)
            fresh.append(ep)
        if seen == set(self._candidates):
            return
        self._candidates = _order_endpoints(fresh, first=self.endpoint)
        log.info(
            "PeerLink %s: адреса соседа (по последнему hello) — %s",
            self.name,
            ", ".join(str(ep) for ep in self._candidates),
        )
        if self._on_endpoints is not None:
            self._on_endpoints(self.name, self.endpoints)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"peer-link-{self.name}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def forward(self, env: Envelope) -> Envelope:
        client = self._client
        if client is None:
            raise ProtoError(ERR_UNAVAILABLE, f"{self.name} сейчас недоступна")
        try:
            return await client.forward(env)
        except TimeoutError as exc:
            raise ProtoError(ERR_TIMEOUT, f"{self.name}: нет ответа ({exc})") from exc
        except (ConnectionError, OSError) as exc:
            self.reconnect_now(f"запрос не доехал ({exc})")
            raise ProtoError(ERR_UNAVAILABLE, f"{self.name}: {exc}") from exc

    def note_left(self) -> None:
        self.left = True
        self.reconnect_now("нода уходит штатно")
        self._mark_down()

    def reconnect_now(self, reason: str) -> None:
        client = self._client
        if client is None:
            return
        log.info("PeerLink %s: %s — переподключаюсь немедленно", self.name, reason)
        self._client = None
        self._dropping = True
        client.abort()

    async def _heartbeat(self, client: ProtoClient) -> None:
        misses = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            try:
                await asyncio.wait_for(client.hello(), timeout=self._heartbeat_timeout)
                misses = 0
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                misses += 1
                if misses >= HEARTBEAT_MISSES:
                    log.warning(
                        "PeerLink %s: не отвечает на %d пробы подряд (%s) — рву соединение",
                        self.name,
                        misses,
                        exc,
                    )
                    self._dropping = True
                    client.abort()
                    return

    def _mark_down(self) -> None:
        if self.down_since is None:
            self.down_since = time.monotonic()

    def downtime_s(self) -> float | None:
        if self.alive or self.down_since is None:
            return None
        return time.monotonic() - self.down_since

    async def _dial(self) -> tuple[ProtoClient, ServiceInfo, Endpoint] | None:
        last_error: Exception | None = None
        mismatched: tuple[ProtoClient, ServiceInfo, Endpoint] | None = None
        for ep in list(self._candidates):
            client = ProtoClient(
                ep,
                token=self._token,
                on_event=self._on_event,
                src=self._src,
                incarnation=NODE_INCARNATION,
            )
            try:
                info = await asyncio.wait_for(_handshake(client), timeout=CONNECT_TIMEOUT_S)
            except asyncio.CancelledError:
                client.abort()
                if mismatched is not None:
                    mismatched[0].abort()
                raise
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                last_error = exc
                with contextlib.suppress(Exception):
                    await client.close()
                continue
            if not _answers_to(info, self.name):
                if self._self_node and info.node.lower() == self._self_node:
                    log.warning(
                        "PeerLink %s: на %s отвечаю сам себе — адрес соседа "
                        "устарел, пропускаю",
                        self.name,
                        ep,
                    )
                    with contextlib.suppress(Exception):
                        await client.close()
                    continue
                log.warning(
                    "PeerLink %s: на %s отвечает %s/%s — проверь конфиг",
                    self.name,
                    ep,
                    info.node,
                    info.service,
                )
                if mismatched is None:
                    mismatched = (client, info, ep)
                else:
                    with contextlib.suppress(Exception):
                        await client.close()
                continue
            if mismatched is not None:
                with contextlib.suppress(Exception):
                    await mismatched[0].close()
            return client, info, ep
        if mismatched is not None:
            return mismatched
        self._last_error = last_error
        return None

    async def _serve(self, client: ProtoClient, info: ServiceInfo, endpoint: Endpoint) -> None:
        confirmed = _answers_to(info, self.name)
        if confirmed:
            switched = endpoint != self.endpoint
            if switched:
                log.info("PeerLink %s: путь сменился на %s", self.name, endpoint)
            self.endpoint = endpoint
            self._candidates = _order_endpoints(self._candidates, first=endpoint)
            if switched and self._on_endpoints is not None:
                self._on_endpoints(self.name, self.endpoints)
        log.info("PeerLink %s: связь установлена (%s/%s)", self.name, info.node, info.service)
        if confirmed:
            if info.node_kind:
                self.node_kind = info.node_kind
            if info.wake:
                self.wake_info = info.wake
            if info.endpoints:
                self.learn_endpoints(info.endpoints)
        self.down_since = None
        self.left = False
        self._client = client
        heartbeat = asyncio.create_task(self._heartbeat(client), name=f"peer-link-hb-{self.name}")
        try:
            await client.join()
        except asyncio.CancelledError:
            if not self._dropping or self._stopping:
                raise
        finally:
            self._dropping = False
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            self._client = None
            self._mark_down()
        log.warning("PeerLink %s: связь потеряна, переподключение...", self.name)

    async def _run(self) -> None:
        logged_down = False
        while not self._stopping:
            dialed = None
            try:
                dialed = await self._dial()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("PeerLink %s: непредвиденная ошибка, продолжаю попытки", self.name)
            if dialed is None:
                self._mark_down()
                if not logged_down:
                    log.warning(
                        "PeerLink %s недоступен ни по одному адресу [%s] (%s) — "
                        "переподключение каждые %.0f с",
                        self.name,
                        ", ".join(self.endpoints),
                        self._last_error,
                        self._delay,
                    )
                    logged_down = True
                await asyncio.sleep(self._delay)
                continue
            logged_down = False
            client, info, endpoint = dialed
            try:
                await self._serve(client, info, endpoint)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._mark_down()
                log.exception("PeerLink %s: непредвиденная ошибка, продолжаю попытки", self.name)
            finally:
                self._client = None
                with contextlib.suppress(Exception):
                    await client.close()
            await asyncio.sleep(self._delay)


class NodeRouter:
    def __init__(
        self,
        node_id: str,
        *,
        peers: dict[str, PeerLink] | None = None,
        local_services: dict[str, PeerLink] | None = None,
    ) -> None:
        self.node_id = node_id
        self.peers = peers or {}
        self.local_services = local_services or {}

    async def route(self, request: Envelope) -> Envelope | None:
        dst = request.dst
        if dst is None:
            return None
        if dst.node is not None and dst.node != self.node_id:
            peer = self.peers.get(dst.node)
            if peer is None:
                known = ", ".join(self.peers) or "нет пиров"
                raise ProtoError(ERR_UNKNOWN_DST, f"неизвестная нода: {dst.node} (есть: {known})")
            return await peer.forward(request)
        if dst.service is not None and dst.service != NODE_SERVICE:
            link = self.local_services.get(dst.service)
            if link is None:
                known = ", ".join(self.local_services) or "нет служб"
                raise ProtoError(
                    ERR_UNKNOWN_DST, f"нет такой службы: {dst.service} (есть: {known})"
                )
            return await link.forward(request)
        return None

    def peers_state(self) -> list[dict[str, object]]:
        return [
            {
                "id": link.name,
                "endpoint": str(link.endpoint),
                "endpoints": link.endpoints,
                "alive": link.alive,
                "kind": link.node_kind,
                "down_s": link.downtime_s(),
                "left": link.left,
                "wake": link.wake_info,
            }
            for link in self.peers.values()
        ]

    async def add_local_service(self, name: str, link: PeerLink) -> None:
        self.local_services[name] = link
        await link.start()

    async def remove_local_service(self, name: str) -> None:
        link = self.local_services.pop(name, None)
        if link is not None:
            await link.stop()

    async def add_peer(self, link: PeerLink) -> None:
        self.peers[link.name] = link
        await link.start()

    async def remove_peer(self, node_id: str) -> None:
        link = self.peers.pop(node_id, None)
        if link is not None:
            await link.stop()
