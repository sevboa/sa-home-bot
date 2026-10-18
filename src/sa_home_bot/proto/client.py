from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sa_home_bot.proto.endpoints import Endpoint, TcpEndpoint, UnixEndpoint, parse_endpoint
from sa_home_bot.proto.messages import (
    MAX_MESSAGE_BYTES,
    MSG_AUTH,
    MSG_COMMAND,
    MSG_DESCRIBE,
    MSG_EVENT,
    MSG_GET_STATE,
    MSG_HELLO,
    MSG_RESPONSE,
    Address,
    Envelope,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
    decode,
    encode,
    make_request,
)
from sa_home_bot.proto.tcpopts import enable_tcp_keepalive

log = logging.getLogger(__name__)

EventCallback = Callable[[Envelope], Awaitable[None]]

DEFAULT_TIMEOUT = 10.0

CLOSE_TIMEOUT_S = 5.0


class ProtoClient:
    def __init__(
        self,
        endpoint: str | Path | Endpoint,
        *,
        token: str = "",
        src: Address | None = None,
        incarnation: str = "",
        on_event: EventCallback | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._endpoint = parse_endpoint(endpoint)
        self._token = token
        self._src = src
        self._incarnation = incarnation
        self._on_event = on_event
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._events: asyncio.Queue[Envelope] = asyncio.Queue()
        self._event_task: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[Envelope]] = {}
        self._write_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def connect(self) -> None:
        if isinstance(self._endpoint, UnixEndpoint):
            self._reader, self._writer = await asyncio.open_unix_connection(
                path=str(self._endpoint.path), limit=MAX_MESSAGE_BYTES
            )
        else:
            self._reader, self._writer = await asyncio.open_connection(
                host=self._endpoint.host, port=self._endpoint.port, limit=MAX_MESSAGE_BYTES
            )
            raw_sock = self._writer.get_extra_info("socket")
            if raw_sock is not None:
                enable_tcp_keepalive(raw_sock)
        self._reader_task = asyncio.create_task(self._read_loop(), name="proto-client-reader")
        if self._on_event is not None:
            self._event_task = asyncio.create_task(self._event_loop(), name="proto-client-events")
        if isinstance(self._endpoint, TcpEndpoint):
            auth_payload: dict[str, Any] = {"token": self._token}
            if self._src is not None and self._src.node:
                auth_payload["node"] = self._src.node
                auth_payload["incarnation"] = self._incarnation
            try:
                await self.request(MSG_AUTH, auth_payload)
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None
        await self._stop_events()
        if self._writer is not None:
            writer, self._writer = self._writer, None
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT_S)
        self._reader = None
        self._fail_pending(ConnectionError("клиент закрыт"))

    def abort(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._event_task is not None and self._event_task is not asyncio.current_task():
            self._event_task.cancel()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()

    async def join(self) -> None:
        if self._reader_task is not None:
            await self._reader_task
        await self._drain_events()


    async def hello(self, dst: Address | None = None) -> ServiceInfo:
        payload = await self.request(MSG_HELLO, dst=dst)
        return ServiceInfo.from_payload(payload)

    async def describe(self, dst: Address | None = None) -> ServiceDescription:
        payload = await self.request(MSG_DESCRIBE, dst=dst)
        return ServiceDescription.from_payload(payload)

    async def get_state(self, dst: Address | None = None) -> dict[str, Any]:
        return await self.request(MSG_GET_STATE, dst=dst)

    async def command(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        dst: Address | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            MSG_COMMAND, {"action": action, "args": args or {}}, dst=dst, timeout=timeout
        )

    async def request(
        self,
        type_: str,
        payload: dict[str, Any] | None = None,
        *,
        dst: Address | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        env = make_request(type_, payload, src=self._src, dst=dst, timeout_s=timeout)
        response = await self.forward(env)
        if response.ok is not True:
            raise ProtoError(response.error_code() or "unknown", response.error_message())
        return response.payload

    async def forward(self, env: Envelope) -> Envelope:
        if self._writer is None:
            raise ConnectionError("клиент не подключён")
        if self._reader_task is not None and self._reader_task.done():
            raise ConnectionError("соединение закрыто")
        future: asyncio.Future[Envelope] = asyncio.get_running_loop().create_future()
        self._pending[env.id] = future
        timeout = env.timeout_s if env.timeout_s is not None else self._timeout
        try:
            async with self._write_lock:
                self._writer.write(encode(env))
                await self._writer.drain()
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(env.id, None)


    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                if line.strip() == b"":
                    continue
                try:
                    env = decode(line)
                except ProtoError as exc:
                    log.warning("ProtoClient: невалидное сообщение от сервера: %s", exc)
                    continue
                await self._handle_message(env)
        except (ConnectionError, OSError) as exc:
            log.warning("ProtoClient: соединение оборвалось: %s", exc)
        finally:
            self._fail_pending(ConnectionError("соединение закрыто"))

    async def _handle_message(self, env: Envelope) -> None:
        if env.type == MSG_RESPONSE:
            future = self._pending.get(env.id)
            if future is not None and not future.done():
                future.set_result(env)
            return
        if env.type == MSG_EVENT:
            if self._on_event is not None:
                self._events.put_nowait(env)
            return
        log.warning("ProtoClient: неожиданный тип сообщения от сервера: %s", env.type)

    async def _event_loop(self) -> None:
        assert self._on_event is not None
        while self._event_task is asyncio.current_task():
            env = await self._events.get()
            try:
                await self._on_event(env)
            except Exception:
                log.exception("ProtoClient: обработчик события упал")
            self._events.task_done()

    async def _drain_events(self) -> None:
        task = self._event_task
        if task is None or task is asyncio.current_task():
            return
        drained = asyncio.create_task(self._events.join())
        try:
            await asyncio.wait({drained, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            drained.cancel()

    async def _stop_events(self) -> None:
        task, self._event_task = self._event_task, None
        if task is None:
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
