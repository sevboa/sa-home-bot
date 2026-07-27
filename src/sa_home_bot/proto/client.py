"""Клиент протокола v0: запрос-ответ по id + приём событий.

Одно подключение к своей локальной ноде/службе (unix-сокет или TCP; на TCP
`connect()` сам проходит auth токеном). Фоновая задача читает сокет: ответы
резолвят ожидающие future по id запроса, события уходят в callback
`on_event`. Падение callback'а не валит читателя. Переподключение — забота
вызывающего (этап 13: бот переживает обрыв и реконнектится).
"""

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

# Потолок на закрытие соединения (см. close): полумёртвый сокет не должен
# задерживать переподключение.
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
        # Инкарнация нашего процесса (см. node/peers.py::NODE_INCARNATION):
        # едет в auth, чтобы сосед отличил наш перезапуск от переподключения.
        self._incarnation = incarnation
        self._on_event = on_event
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
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
        if isinstance(self._endpoint, TcpEndpoint):
            # TCP требует auth первым сообщением; неверный токен → ProtoError.
            # Заодно представляемся: по имени узла сервер понимает, что сосед
            # ПЕРЕподключился, и роняет свой протухший исходящий линк к нему
            # (см. node/peers.py::reconnect_now). Поле необязательное — нода
            # старой версии просто его не заметит.
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
        if self._writer is not None:
            writer, self._writer = self._writer, None
            writer.close()
            # Живая находка 2026-07-28: без таймаута `wait_closed()` виснет на
            # полумёртвом сокете (данные в Send-Q не уходят, FIN не
            # подтверждается) — и вешает весь цикл переподключения PeerLink,
            # который зовёт close() перед новой попыткой. Сокет всё равно уже
            # closed(); дожидаться подтверждения не обязательно.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=CLOSE_TIMEOUT_S)
        self._reader = None
        self._fail_pending(ConnectionError("клиент закрыт"))

    def abort(self) -> None:
        """Синхронно оборвать соединение, не дожидаясь закрытия.

        Роняет читающую задачу — а с ней и `join()` у того, кто держит этот
        клиент; полное `close()` делает уже он, в одном месте. Так убран
        источник гонки: раньше `close()` звали параллельно из heartbeat и из
        reconnect_now, и они переплетались на общих полях (живая находка
        2026-07-28: линк после этого не переподключался вовсе).
        """
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()

    async def join(self) -> None:
        """Дождаться завершения фоновой читающей задачи (EOF/обрыв/закрытие)."""
        if self._reader_task is not None:
            await self._reader_task

    # --- Запросы ---

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
        """Отправить запрос и дождаться ответа. ProtoError при ok=False.

        ``timeout`` — переопределить таймаут ожидания для этого конкретного
        запроса (едет в конверте как ``timeout_s`` и переживает форвард через
        чужие ноды, см. докстринг `Envelope`); без него — `self._timeout`.
        """
        env = make_request(type_, payload, src=self._src, dst=dst, timeout_s=timeout)
        response = await self.forward(env)
        if response.ok is not True:
            raise ProtoError(response.error_code() or "unknown", response.error_message())
        return response.payload

    async def forward(self, env: Envelope) -> Envelope:
        """Переслать готовый конверт и вернуть весь конверт ответа (id сохраняется).

        Для маршрутизации нодой: чужой запрос уходит как есть, ответ (включая
        ok=False) возвращается конвертом, а не исключением — нода отдаёт его
        исходному клиенту без переупаковки.
        """
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

    # --- Чтение сокета ---

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF — сервер закрыл соединение
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
            if self._on_event is None:
                return
            try:
                await self._on_event(env)
            except Exception:  # noqa: BLE001 — callback не должен валить читателя
                log.exception("ProtoClient: обработчик события упал")
            return
        log.warning("ProtoClient: неожиданный тип сообщения от сервера: %s", env.type)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
