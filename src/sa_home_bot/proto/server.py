"""Сервер протокола v0: unix-сокет или TCP, NDJSON, рассылка событий подключённым.

Сервер обслуживает одну службу (`ServiceHandler`): hello/describe отвечает из
её описания, get_state и command делегирует ей. Валидация команды (известность
действия, обязательные параметры) — по `describe`, а не по захардкоженному
списку. Падение обработчика одного запроса не валит ни соединение, ни сервер.

Транспорт: unix-сокет доверяет правам файла (0600); TCP требует токен —
первое сообщение соединения обязано быть `auth {token}`, всё прочее до него
(и неверный токен) получает `unauthorized` и разрыв. События уходят только
аутентифицированным соединениям.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from pathlib import Path
from typing import Any, Protocol

from sa_home_bot.proto.endpoints import Endpoint, TcpEndpoint, UnixEndpoint, parse_endpoint
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

log = logging.getLogger(__name__)


class ServiceHandler(Protocol):
    """Что должна уметь служба, чтобы её можно было выставить по протоколу."""

    def describe(self) -> ServiceDescription: ...

    async def get_state(self) -> dict[str, Any]: ...

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]: ...


class _Connection:
    """Одно клиентское подключение: writer + лок, чтобы ответы и broadcast
    событий не перемешивались в сокете."""

    def __init__(self, writer: asyncio.StreamWriter, *, authenticated: bool) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()
        self.authenticated = authenticated

    async def send(self, env: Envelope) -> None:
        async with self.lock:
            self.writer.write(encode(env))
            await self.writer.drain()


class ProtoServer:
    def __init__(
        self,
        endpoint: str | Path | Endpoint,
        handler: ServiceHandler,
        *,
        token: str = "",
    ) -> None:
        self._endpoint = parse_endpoint(endpoint)
        if isinstance(self._endpoint, TcpEndpoint) and not token:
            raise ValueError(
                f"TCP-endpoint {self._endpoint} требует токен ([swarm].token в конфиге)"
            )
        self._token = token
        self._handler = handler
        self._server: asyncio.Server | None = None
        self._connections: set[_Connection] = set()

    @property
    def endpoint(self) -> Endpoint:
        """Фактический endpoint (для tcp с портом 0 — после start() реальный порт)."""
        return self._endpoint

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def start(self) -> None:
        if isinstance(self._endpoint, UnixEndpoint):
            path = self._endpoint.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)  # хвост от прошлого запуска
            self._server = await asyncio.start_unix_server(
                self._handle_client, path=str(path), limit=MAX_MESSAGE_BYTES
            )
            path.chmod(0o600)
        else:
            self._server = await asyncio.start_server(
                self._handle_client,
                host=self._endpoint.host,
                port=self._endpoint.port,
                limit=MAX_MESSAGE_BYTES,
            )
            if self._endpoint.port == 0:  # порт выбрала ОС (тесты) — узнать реальный
                bound = self._server.sockets[0].getsockname()
                self._endpoint = TcpEndpoint(self._endpoint.host, bound[1])
        log.info("ProtoServer слушает %s", self._endpoint)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        # Сначала закрыть живые соединения: с Python 3.12 wait_closed() ждёт
        # завершения обработчиков, а те висят на readline() до закрытия сокета.
        for conn in list(self._connections):
            conn.writer.close()
            with contextlib.suppress(Exception):
                await conn.writer.wait_closed()
        self._connections.clear()
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        if isinstance(self._endpoint, UnixEndpoint):
            self._endpoint.path.unlink(missing_ok=True)
        log.info("ProtoServer остановлен")

    async def broadcast_event(self, event_type: str, data: dict[str, Any] | None = None) -> int:
        """Разослать событие всем подключённым клиентам.

        Возвращает число соединений, в которые событие реально записалось, —
        вызывающий по нему решает, считать ли событие доставленным.
        """
        info = self._handler.describe().info
        env = make_event(event_type, data, src=Address(node=info.node, service=info.service))
        delivered = 0
        for conn in list(self._connections):
            if not conn.authenticated:
                continue
            try:
                await conn.send(env)
                delivered += 1
            except (ConnectionError, OSError):
                self._connections.discard(conn)
        return delivered

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Unix-сокет доверяет правам файла; на TCP доверие даёт только auth.
        conn = _Connection(writer, authenticated=isinstance(self._endpoint, UnixEndpoint))
        self._connections.add(conn)
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    log.warning("ProtoServer: сообщение длиннее лимита, закрываю соединение")
                    break
                if not line:
                    break  # EOF — клиент отключился
                if line.strip() == b"":
                    continue
                await self._handle_line(conn, line)
        except (ConnectionError, OSError):
            pass  # обрыв клиента — норма
        finally:
            self._connections.discard(conn)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

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
                    response = await self._dispatch(request)
            except ProtoError as exc:
                close_after = exc.code == ERR_UNAUTHORIZED
                request_id = request.id if request is not None else "?"
                response = make_error_response(request_id, exc.code, exc.message)
            except Exception:
                log.exception("ProtoServer: обработчик запроса упал")
                request_id = request.id if request is not None else "?"
                response = make_error_response(request_id, ERR_INTERNAL, "внутренняя ошибка")
            await conn.send(response)
            if close_after:
                conn.writer.close()
        except (ConnectionError, OSError):
            self._connections.discard(conn)

    def _authenticate(self, conn: _Connection, request: Envelope) -> Envelope:
        if conn.authenticated:  # unix или повторный auth — токен не проверяем
            return make_response(request, {"authenticated": True})
        token = request.payload.get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
            log.warning("ProtoServer: отвергнут клиент с неверным токеном (%s)", self._endpoint)
            raise ProtoError(ERR_UNAUTHORIZED, "неверный токен")
        conn.authenticated = True
        return make_response(request, {"authenticated": True})

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


def decode_request(line: bytes) -> Envelope:
    """Декодировать строку и убедиться, что это запрос."""
    env = decode(line)
    if env.type not in REQUEST_TYPES:
        raise ProtoError(ERR_UNKNOWN_TYPE, f"ожидался запрос, пришёл {env.type}")
    return env
