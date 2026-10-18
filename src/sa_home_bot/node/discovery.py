from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from hashlib import sha256

from sa_home_bot.config import DiscoveryConfig
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.proto.endpoints import (
    is_loopback,
    local_broadcast_addresses,
    parse_endpoint,
)
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

DISCOVERY_V = 0

MAX_DATAGRAM_BYTES = 1024

TYPE_QUERY = "q"
TYPE_REPLY = "r"

REMEMBERED_NONCES = 4

MAX_REPLY_ENDPOINTS = 8

JoinCallable = Callable[[str], Awaitable[dict]]


def _canonical(body: dict) -> bytes:
    payload = {k: v for k, v in body.items() if k != "sig"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def sign(token: str, body: dict) -> str:
    return hmac.new(token.encode(), _canonical(body), sha256).hexdigest()


def encode(token: str, body: dict) -> bytes:
    signed = {**body, "sig": sign(token, body)}
    return json.dumps(signed, separators=(",", ":")).encode()


def make_query(node_id: str, nonce: str) -> dict:
    return {"v": DISCOVERY_V, "t": TYPE_QUERY, "node": node_id, "nonce": nonce}


def make_reply(node_id: str, nonce: str, endpoints: Sequence[str], kind: str = "") -> dict:
    return {
        "v": DISCOVERY_V,
        "t": TYPE_REPLY,
        "node": node_id,
        "nonce": nonce,
        "kind": kind,
        "endpoints": list(endpoints),
    }


def parse(data: bytes, token: str) -> dict | None:
    if not token or len(data) > MAX_DATAGRAM_BYTES:
        return None
    try:
        body = json.loads(data.decode())
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    if body.get("v") != DISCOVERY_V or body.get("t") not in (TYPE_QUERY, TYPE_REPLY):
        return None
    node, nonce, got = body.get("node"), body.get("nonce"), body.get("sig")
    if not isinstance(node, str) or not node:
        return None
    if not isinstance(nonce, str) or not nonce:
        return None
    if not isinstance(got, str) or not hmac.compare_digest(got, sign(token, body)):
        return None
    return body


def usable_endpoints(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values[:MAX_REPLY_ENDPOINTS]:
        if not isinstance(value, str) or not value:
            continue
        try:
            endpoint = parse_endpoint(value)
        except ValueError:
            continue
        if is_loopback(endpoint):
            continue
        if value not in out:
            out.append(value)
    return out


def next_interval(links: Iterable, cfg: DiscoveryConfig) -> float:
    peers = list(links)
    if not peers or any(not link.alive for link in peers):
        return cfg.active_interval_s
    return cfg.idle_interval_s


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, owner: SwarmDiscovery) -> None:
        self._owner = owner

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._owner.handle_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("discovery: ошибка UDP: %s", exc)


class SwarmDiscovery:
    def __init__(
        self,
        node_id: str,
        router: NodeRouter,
        *,
        token: str,
        join: JoinCallable,
        advertise: Callable[[], list[str]],
        cfg: DiscoveryConfig | None = None,
        node_kind: str = "",
        port: int | None = None,
        targets: Callable[[], list[tuple[str, int]]] | None = None,
    ) -> None:
        self._node_id = node_id
        self._router = router
        self._token = token
        self._join = join
        self._advertise = advertise
        self._cfg = cfg or DiscoveryConfig()
        self._node_kind = node_kind
        self._targets = targets or self._broadcast_targets
        self._transport: asyncio.DatagramTransport | None = None
        self._task: asyncio.Task | None = None
        self._nonces: list[str] = []
        self._joining: set[str] = set()
        self._jobs: set[asyncio.Task] = set()
        self._port = self._cfg.port if port is None else port
        self._last_query_at: float | None = None
        self._replies_seen = 0
        self._found: set[str] = set()

    @property
    def running(self) -> bool:
        return self._transport is not None

    @property
    def port(self) -> int:
        return self._port

    def _broadcast_targets(self) -> list[tuple[str, int]]:
        addrs = local_broadcast_addresses()
        return [(addr, self._cfg.port) for addr in addrs or ["255.255.255.255"]]

    async def start(self) -> None:
        if not self._cfg.enabled:
            log.info("discovery: выключен в конфиге")
            return
        if not self._token:
            log.warning("discovery: не запущен — пустой [swarm].token, подписывать нечем")
            return
        loop = asyncio.get_running_loop()
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Protocol(self),
                local_addr=("0.0.0.0", self._port),
                allow_broadcast=True,
            )
        except OSError as exc:
            log.warning(
                "discovery: не занять UDP-порт %s (%s) — работаем без маячка", self._port, exc
            )
            return
        self._transport = transport
        sock = transport.get_extra_info("sockname")
        if sock:
            self._port = sock[1]
        self._task = asyncio.create_task(self._run(), name="swarm-discovery")
        log.info("discovery: маячок роя на UDP :%s", self._port)

    async def stop(self) -> None:
        await cancel_task(self._task)
        self._task = None
        for job in list(self._jobs):
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job
        self._jobs.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def state(self) -> dict:
        return {
            "enabled": self._cfg.enabled,
            "running": self.running,
            "port": self._port,
            "last_query_at": self._last_query_at,
            "replies_seen": self._replies_seen,
            "peers_found": sorted(self._found),
        }

    def handle_datagram(self, data: bytes, addr: tuple) -> None:
        body = parse(data, self._token)
        if body is None:
            return
        node = body["node"]
        if node == self._node_id:
            return
        if body["t"] == TYPE_QUERY:
            self._reply_to(body["nonce"], addr)
            return
        if body["nonce"] not in self._nonces:
            return
        self._replies_seen += 1
        self._spawn(self._handle_reply(node, body))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    def _reply_to(self, nonce: str, addr: tuple) -> None:
        endpoints = self._advertise()
        if not endpoints or self._transport is None:
            return
        body = make_reply(self._node_id, nonce, endpoints, self._node_kind)
        self._transport.sendto(encode(self._token, body), addr)

    async def _handle_reply(self, node: str, body: dict) -> None:
        endpoints = usable_endpoints(body.get("endpoints"))
        if not endpoints:
            return
        self._found.add(node)
        link = self._router.peers.get(node)
        if link is None:
            await self._join_found(node, endpoints)
            return
        if link.alive:
            return
        fresh = [e for e in endpoints if e not in link.endpoints]
        if not fresh:
            return
        link.learn_endpoints(endpoints)
        link.reconnect_now(f"discovery: новый адрес {endpoints[0]}")
        log.info("discovery: у пира %s новый адрес %s", node, endpoints[0])

    async def _join_found(self, node: str, endpoints: Sequence[str]) -> None:
        if node in self._joining:
            return
        self._joining.add(node)
        try:
            for endpoint in endpoints:
                try:
                    result = await self._join(endpoint)
                except ProtoError as exc:
                    log.debug("discovery: join к %s через %s не вышел: %s", node, endpoint, exc)
                    continue
                log.info(
                    "discovery: нашли ноду %s (%s), присоединились, новых пиров: %s",
                    node,
                    endpoint,
                    result.get("peers_added", []),
                )
                return
            log.warning(
                "discovery: нода %s отозвалась, но join не удался ни по одному адресу", node
            )
        finally:
            self._joining.discard(node)

    def query_once(self) -> None:
        if self._transport is None:
            return
        nonce = secrets.token_hex(8)
        self._nonces.append(nonce)
        del self._nonces[:-REMEMBERED_NONCES]
        packet = encode(self._token, make_query(self._node_id, nonce))
        for host, port in self._targets():
            try:
                self._transport.sendto(packet, (host, port))
            except OSError as exc:
                log.debug("discovery: не отправить запрос на %s:%s (%s)", host, port, exc)
        self._last_query_at = time.time()

    async def _run(self) -> None:
        self.query_once()
        while True:
            await asyncio.sleep(next_interval(self._router.peers.values(), self._cfg))
            try:
                self.query_once()
            except Exception:
                log.exception("discovery: ошибка рассылки запроса")
