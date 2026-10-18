from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import hmac
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from sa_home_bot.proto.endpoints import (
    Endpoint,
    TcpEndpoint,
    UnixEndpoint,
    advertisable,
    parse_endpoint,
)
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_UNAUTHORIZED,
    ERR_UNKNOWN_ACTION,
    ERR_UNKNOWN_TYPE,
    MAX_MESSAGE_BYTES,
    MSG_AUTH,
    MSG_COMMAND,
    MSG_DESCRIBE,
    MSG_GET_STATE,
    MSG_HELLO,
    REQUEST_TYPES,
    Address,
    Envelope,
    ProtoError,
    ServiceDescription,
    decode,
    encode,
    make_error_response,
    make_event,
    make_response,
)
from sa_home_bot.proto.tcpopts import enable_tcp_keepalive

log = logging.getLogger(__name__)


class ServiceHandler(Protocol):
    def describe(self) -> ServiceDescription: ...

    async def get_state(self) -> dict[str, Any]: ...

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]: ...


Router = Callable[[Envelope], Awaitable[Envelope | None]]

BIND_RETRY_TIMEOUT_S = 180.0
BIND_RETRY_INTERVAL_S = 3.0
BIND_RETRY_SLOW_INTERVAL_S = 30.0

SEND_TIMEOUT_S = 10.0

AUTH_TIMEOUT_S = 10.0

CLOSE_TIMEOUT_S = 5.0


class _Connection:
    def __init__(self, writer: asyncio.StreamWriter, *, authenticated: bool) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()
        self.authenticated = authenticated
        self.node: str = ""

    async def send(self, env: Envelope) -> None:
        async with self.lock:
            self.writer.write(encode(env))
            await self.writer.drain()


class ProtoServer:
    def __init__(
        self,
        endpoint: str | Path | Endpoint | Sequence[str | Path | Endpoint],
        handler: ServiceHandler,
        *,
        token: str = "",
        router: Router | None = None,
        on_peer_connect: Callable[[str], None] | None = None,
    ) -> None:
        single = isinstance(endpoint, (str, Path, UnixEndpoint, TcpEndpoint))
        raw = [endpoint] if single else list(endpoint)
        if not raw:
            raise ValueError("нужен хотя бы один endpoint")
        self._endpoints = [parse_endpoint(e) for e in raw]
        for ep in self._endpoints:
            if isinstance(ep, TcpEndpoint) and not token:
                raise ValueError(f"TCP-endpoint {ep} требует токен ([swarm].token в конфиге)")
        self._token = token
        self._handler = handler
        self._router = router
        self._on_peer_connect = on_peer_connect
        self._peer_incarnations: dict[str, str] = {}
        self._bound: dict[int, asyncio.Server] = {}
        self._bind_task: asyncio.Task | None = None
        self._stopping = False
        self._connections: set[_Connection] = set()
        self._request_tasks: set[asyncio.Task] = set()

    @property
    def endpoint(self) -> Endpoint:
        return self._endpoints[0]

    @property
    def endpoints(self) -> tuple[Endpoint, ...]:
        return tuple(self._endpoints)

    @property
    def bound_endpoints(self) -> tuple[Endpoint, ...]:
        return tuple(self._endpoints[i] for i in sorted(self._bound))

    def advertised_endpoints(self) -> list[str]:
        return advertisable(self.bound_endpoints)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def _bind_one(self, index: int) -> None:
        ep = self._endpoints[index]
        if isinstance(ep, UnixEndpoint):
            path = ep.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            server = await asyncio.start_unix_server(
                functools.partial(self._handle_client, trusted=True),
                path=str(path),
                limit=MAX_MESSAGE_BYTES,
            )
            path.chmod(0o600)
        else:
            server = await asyncio.start_server(
                functools.partial(self._handle_client, trusted=False),
                host=ep.host,
                port=ep.port,
                limit=MAX_MESSAGE_BYTES,
            )
            if ep.port == 0:
                self._endpoints[index] = TcpEndpoint(ep.host, server.sockets[0].getsockname()[1])
        self._bound[index] = server
        log.info("ProtoServer слушает %s", self._endpoints[index])

    async def start(self) -> None:
        pending: list[int] = []
        for i in range(len(self._endpoints)):
            try:
                await self._bind_one(i)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    raise
                log.warning("ProtoServer: адреса %s ещё нет (%s)", self._endpoints[i], exc)
                pending.append(i)
        if pending and not self._bound:
            pending = await self._wait_for_first(pending)
        if pending:
            self._bind_task = asyncio.create_task(self._bind_later(pending), name="proto-bind")

    async def _wait_for_first(self, pending: list[int]) -> list[int]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BIND_RETRY_TIMEOUT_S
        log.warning(
            "ProtoServer: нет ни одного адреса — жду появления до %.0f с "
            "(обычно поднимается Tailscale)",
            BIND_RETRY_TIMEOUT_S,
        )
        last_exc: OSError | None = None
        while True:
            await asyncio.sleep(BIND_RETRY_INTERVAL_S)
            for i in list(pending):
                try:
                    await self._bind_one(i)
                    pending.remove(i)
                except OSError as exc:
                    if exc.errno == errno.EADDRINUSE:
                        raise
                    last_exc = exc
            if self._bound:
                return pending
            if loop.time() >= deadline:
                assert last_exc is not None
                raise last_exc

    async def _bind_later(self, pending: list[int]) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BIND_RETRY_TIMEOUT_S
        while pending:
            interval = (
                BIND_RETRY_INTERVAL_S if loop.time() < deadline else BIND_RETRY_SLOW_INTERVAL_S
            )
            await asyncio.sleep(interval)
            for i in list(pending):
                try:
                    await self._bind_one(i)
                    pending.remove(i)
                except OSError:
                    continue

    async def stop(self) -> None:
        self._stopping = True
        if self._bind_task is not None:
            self._bind_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bind_task
            self._bind_task = None
        for server in self._bound.values():
            server.close()
        for task in list(self._request_tasks):
            task.cancel()
        if self._request_tasks:
            await asyncio.gather(*self._request_tasks, return_exceptions=True)
        conns = list(self._connections)
        for conn in conns:
            conn.writer.close()
        if conns:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(
                        *(conn.writer.wait_closed() for conn in conns), return_exceptions=True
                    ),
                    timeout=CLOSE_TIMEOUT_S,
                )
        self._connections.clear()
        for server in self._bound.values():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=CLOSE_TIMEOUT_S)
        self._bound.clear()
        for ep in self._endpoints:
            if isinstance(ep, UnixEndpoint):
                ep.path.unlink(missing_ok=True)
        log.info("ProtoServer остановлен")

    async def broadcast_event(self, event_type: str, data: dict[str, Any] | None = None) -> int:
        info = self._handler.describe().info
        env = make_event(event_type, data, src=Address(node=info.node, service=info.service))
        return await self.broadcast_envelope(env)

    async def broadcast_envelope(self, env: Envelope) -> int:
        conns = [conn for conn in self._connections if conn.authenticated]
        if not conns:
            return 0
        results = await asyncio.gather(*(self._send_bounded(conn, env) for conn in conns))
        return sum(results)

    async def _send_bounded(self, conn: _Connection, env: Envelope) -> bool:
        try:
            await asyncio.wait_for(conn.send(env), timeout=SEND_TIMEOUT_S)
            return True
        except TimeoutError:
            log.warning(
                "ProtoServer: запись клиенту %s не прошла за %.0f с — закрываю соединение",
                conn.node or conn.writer.get_extra_info("peername"),
                SEND_TIMEOUT_S,
            )
            self._connections.discard(conn)
            with contextlib.suppress(Exception):
                conn.writer.close()
            return False
        except (ConnectionError, OSError):
            self._connections.discard(conn)
            return False

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        trusted: bool,
    ) -> None:
        if self._stopping:
            writer.close()
            return
        conn = _Connection(writer, authenticated=trusted)
        if not trusted:
            raw_sock = writer.get_extra_info("socket")
            if raw_sock is not None:
                enable_tcp_keepalive(raw_sock)
        self._connections.add(conn)
        try:
            while True:
                try:
                    if conn.authenticated:
                        line = await reader.readline()
                    else:
                        line = await asyncio.wait_for(reader.readline(), AUTH_TIMEOUT_S)
                except TimeoutError:
                    log.warning(
                        "ProtoServer: клиент %s не прислал auth за %.0f с — закрываю",
                        writer.get_extra_info("peername"),
                        AUTH_TIMEOUT_S,
                    )
                    break
                except (asyncio.LimitOverrunError, ValueError):
                    log.warning("ProtoServer: сообщение длиннее лимита, закрываю соединение")
                    break
                if not line:
                    break
                if line.strip() == b"":
                    continue
                if self._stopping:
                    break
                task = asyncio.create_task(self._handle_line(conn, line))
                self._request_tasks.add(task)
                task.add_done_callback(self._request_tasks.discard)
        except (ConnectionError, OSError):
            pass
        finally:
            self._connections.discard(conn)
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT_S)

    async def _handle_line(self, conn: _Connection, line: bytes) -> None:
        try:
            request = None
            close_after = False
            try:
                request = decode_request(line)
                if request.type == MSG_AUTH:
                    response = self._authenticate(conn, request)
                elif not conn.authenticated:
                    raise ProtoError(ERR_UNAUTHORIZED, "сначала auth с токеном")
                else:
                    response = None
                    if self._router is not None:
                        response = await self._router(request)
                    if response is None:
                        response = await self._dispatch(request)
            except ProtoError as exc:
                close_after = exc.code == ERR_UNAUTHORIZED
                request_id = request.id if request is not None else "?"
                response = make_error_response(request_id, exc.code, exc.message)
            except Exception:
                log.exception("ProtoServer: обработчик запроса упал")
                request_id = request.id if request is not None else "?"
                response = make_error_response(request_id, ERR_INTERNAL, "внутренняя ошибка")
            await asyncio.wait_for(conn.send(response), timeout=SEND_TIMEOUT_S)
            if close_after:
                conn.writer.close()
        except TimeoutError:
            self._connections.discard(conn)
            with contextlib.suppress(Exception):
                conn.writer.close()
        except (ConnectionError, OSError):
            self._connections.discard(conn)

    def _authenticate(self, conn: _Connection, request: Envelope) -> Envelope:
        if conn.authenticated:
            self._note_peer(conn, request)
            return make_response(request, {"authenticated": True})
        token = request.payload.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            peer = conn.writer.get_extra_info("peername")
            log.warning("ProtoServer: отвергнут клиент с неверным токеном (%s)", peer)
            raise ProtoError(ERR_UNAUTHORIZED, "неверный токен")
        conn.authenticated = True
        self._note_peer(conn, request)
        return make_response(request, {"authenticated": True})

    def _note_peer(self, conn: _Connection, request: Envelope) -> None:
        node = request.payload.get("node")
        if not isinstance(node, str) or not node:
            return
        conn.node = node
        incarnation = request.payload.get("incarnation")
        if not isinstance(incarnation, str):
            incarnation = ""
        previous = self._peer_incarnations.get(node)
        self._peer_incarnations[node] = incarnation
        if previous is None or previous == incarnation:
            return
        for other in [c for c in self._connections if c is not conn and c.node == node]:
            self._connections.discard(other)
            other.writer.close()
        if self._on_peer_connect is not None:
            self._on_peer_connect(node)

    async def _dispatch(self, request: Envelope) -> Envelope:
        if request.type == MSG_HELLO:
            return make_response(request, self._handler.describe().info.to_payload())
        if request.type == MSG_DESCRIBE:
            return make_response(request, self._handler.describe().to_payload())
        if request.type == MSG_GET_STATE:
            return make_response(request, await self._handler.get_state())
        if request.type == MSG_COMMAND:
            return await self._run_command(request)
        raise ProtoError(ERR_UNKNOWN_TYPE, f"неизвестный тип запроса: {request.type}")

    async def _run_command(self, request: Envelope) -> Envelope:
        action_id = request.payload.get("action")
        if not isinstance(action_id, str) or not action_id:
            raise ProtoError(ERR_BAD_REQUEST, "command без action")
        args = request.payload.get("args", {})
        if not isinstance(args, dict):
            raise ProtoError(ERR_BAD_REQUEST, "args должен быть объектом")

        spec = self._handler.describe().find_action(action_id)
        if spec is None:
            raise ProtoError(ERR_UNKNOWN_ACTION, f"нет такого действия: {action_id}")
        for param in spec.params:
            if param.required and param.name not in args:
                raise ProtoError(ERR_BAD_REQUEST, f"нет обязательного параметра: {param.name}")

        result = await self._handler.run_command(action_id, args)
        return make_response(request, result)


def undeclared_action(action: str) -> ValueError:
    return ValueError(f"необъявленное действие: {action}")


def decode_request(line: bytes) -> Envelope:
    env = decode(line)
    if env.type not in REQUEST_TYPES:
        raise ProtoError(ERR_UNKNOWN_TYPE, f"ожидался запрос, пришёл {env.type}")
    return env
