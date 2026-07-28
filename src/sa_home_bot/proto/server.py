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
    """Что должна уметь служба, чтобы её можно было выставить по протоколу."""

    def describe(self) -> ServiceDescription: ...

    async def get_state(self) -> dict[str, Any]: ...

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]: ...


# Маршрутизатор запросов (сервис ноды): вернул конверт — это ответ (запрос был
# переслан по dst), вернул None — запрос локальный, обрабатывает handler.
Router = Callable[[Envelope], Awaitable[Envelope | None]]

# Сколько ждать появления собственного адреса при старте, когда не поднялся
# НИ ОДИН (см. start). С запасом больше типичных 40-60 с подъёма Tailscale:
# лучше подождать лишнее, чем не подняться совсем.
BIND_RETRY_TIMEOUT_S = 180.0
BIND_RETRY_INTERVAL_S = 3.0
# Насколько редко добирать оставшиеся адреса, когда ждать уже некуда: нода
# работает, слушатель у неё есть, недостающий адрес — лишний путь, а не
# жизненная необходимость. Раз в полминуты хватит и логи не засоряет.
BIND_RETRY_SLOW_INTERVAL_S = 30.0

# Потолок на одну запись в клиентский сокет. Зависание записи — это не
# исключение, а вечный await на drain() полумёртвого соединения: без потолка
# один залипший клиент останавливал ВСЮ рассылку событий, а с ней и emit()
# супервизора/аренды/репликации — жизненный цикл служб ноды.
SEND_TIMEOUT_S = 10.0

# Сколько TCP-клиенту дано на первое сообщение (auth): молчащее
# неаутентифицированное соединение иначе висит в _connections вечно.
AUTH_TIMEOUT_S = 10.0

# Потолок на ожидание закрытия слушателя. С Python 3.12 `wait_closed()` ждёт
# завершения ВСЕХ принятых обработчиков — то есть заложник любого клиента,
# который не отпустил сокет. Останов ноды не должен зависеть от чужой
# доброй воли (тот же принцип, что у ProtoClient.CLOSE_TIMEOUT_S).
CLOSE_TIMEOUT_S = 5.0


class _Connection:
    """Одно клиентское подключение: writer + лок, чтобы ответы и broadcast
    событий не перемешивались в сокете."""

    def __init__(self, writer: asyncio.StreamWriter, *, authenticated: bool) -> None:
        self.writer = writer
        self.lock = asyncio.Lock()
        self.authenticated = authenticated
        # Имя узла клиента из auth (см. proto/client.py::connect) — пусто у
        # локальных фронтендов и у нод старых версий.
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
        # Нода слушает и unix (локальные фронтенды), и tcp (пиры) — список.
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
        # Зовётся, когда сосед аутентифицировался и назвался: повод считать
        # собственный исходящий линк к нему протухшим (он явно перезапустился).
        self._on_peer_connect = on_peer_connect
        # Последняя известная инкарнация каждого соседа: смена = он
        # перезапустился (см. _note_peer).
        self._peer_incarnations: dict[str, str] = {}
        # Слушатели по индексу endpoint'а: адрес может подняться позже
        # остальных (tailscale) — тогда его слот пуст, а нода уже работает.
        self._bound: dict[int, asyncio.Server] = {}
        self._bind_task: asyncio.Task | None = None
        # Останов начался: обработчик соединения, принятого в этот момент, не
        # должен начинать работу (см. stop и _handle_client).
        self._stopping = False
        self._connections: set[_Connection] = set()
        self._request_tasks: set[asyncio.Task] = set()

    @property
    def endpoint(self) -> Endpoint:
        """Первый endpoint (для tcp с портом 0 — после start() реальный порт)."""
        return self._endpoints[0]

    @property
    def endpoints(self) -> tuple[Endpoint, ...]:
        """Все настроенные адреса — включая те, что ещё не поднялись."""
        return tuple(self._endpoints)

    @property
    def bound_endpoints(self) -> tuple[Endpoint, ...]:
        """Адреса, на которых сервер СЕЙЧАС реально слушает."""
        return tuple(self._endpoints[i] for i in sorted(self._bound))

    def advertised_endpoints(self) -> list[str]:
        """Что объявлять рою в hello: реально слушаемые TCP-адреса, без
        loopback, с раскрытым ``0.0.0.0`` (см. proto/endpoints.advertisable).
        """
        return advertisable(self.bound_endpoints)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def _bind_one(self, index: int) -> None:
        """Одна попытка поднять слушатель на ``self._endpoints[index]``.

        OSError наружу — вызывающий решает, ждать адрес или сдаться.
        """
        ep = self._endpoints[index]
        if isinstance(ep, UnixEndpoint):
            path = ep.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)  # хвост от прошлого запуска
            server = await asyncio.start_unix_server(
                # unix доверяет правам файла — auth соединению не нужен
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
            if ep.port == 0:  # порт выбрала ОС (тесты) — узнать реальный
                self._endpoints[index] = TcpEndpoint(ep.host, server.sockets[0].getsockname()[1])
        self._bound[index] = server
        log.info("ProtoServer слушает %s", self._endpoints[index])

    async def start(self) -> None:
        """Поднять слушатели. Адрес, которого ещё нет, ноду не блокирует.

        Живая находка 2026-07-28: на winpc нода падала на старте с
        ``could not bind on any address out of [('100.78.225.83', 8710)]`` —
        служба Windows поднимается за секунды, а Tailscale отдаёт свой адрес
        только через 40-60 с (ему нужно достучаться до координационного
        сервера и поднять туннель). WinSW отрабатывал два ретрая и сдавался:
        обёртка числилась RUNNING, а ноды в рое не было до ручного рестарта.

        Ждать такой адрес нужно, только пока не поднялось НИЧЕГО: нода без
        единого слушателя бесполезна. Если хоть один адрес есть (unix-сокет
        или LAN — этап 24), нода стартует немедленно и работает, а
        недостающие адреса добираются фоном — ровно так домашний рой
        собирается по LAN, не дожидаясь ни Tailscale, ни интернета.
        """
        pending: list[int] = []
        for i in range(len(self._endpoints)):
            try:
                await self._bind_one(i)
            except OSError as exc:
                # EADDRINUSE — порт занят кем-то другим, ожидание не поможет.
                if exc.errno == errno.EADDRINUSE:
                    raise
                log.warning("ProtoServer: адреса %s ещё нет (%s)", self._endpoints[i], exc)
                pending.append(i)
        if pending and not self._bound:
            pending = await self._wait_for_first(pending)
        if pending:
            self._bind_task = asyncio.create_task(self._bind_later(pending), name="proto-bind")

    async def _wait_for_first(self, pending: list[int]) -> list[int]:
        """Ни один адрес не поднялся: ждать первого до BIND_RETRY_TIMEOUT_S.
        Вернуть тех, кто так и не поднялся; не поднялся никто — OSError."""
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
        """Добирать оставшиеся адреса фоном — нода уже работает.

        Без дедлайна нарочно: tailscale-адрес может появиться и через час
        (провайдер лежал), а с ним появляется и путь к нодам вне LAN.
        """
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
                    continue  # адреса всё ещё нет — о нём уже сказано в start()

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
        # Сначала закрыть живые соединения: с Python 3.12 wait_closed() ждёт
        # завершения обработчиков, а те висят на readline() до закрытия сокета.
        for conn in list(self._connections):
            conn.writer.close()
            with contextlib.suppress(Exception):
                await conn.writer.wait_closed()
        self._connections.clear()
        for server in self._bound.values():
            # Под потолком: обработчик соединения, принятого прямо в момент
            # останова, в _connections ещё не попал — закрыть его writer было
            # некому, и без потолка мы ждали бы его вечно (живая находка при
            # работе над этапом 24: тест двух нод вис на teardown).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=CLOSE_TIMEOUT_S)
        self._bound.clear()
        for ep in self._endpoints:
            if isinstance(ep, UnixEndpoint):
                ep.path.unlink(missing_ok=True)
        log.info("ProtoServer остановлен")

    async def broadcast_event(self, event_type: str, data: dict[str, Any] | None = None) -> int:
        """Разослать событие всем подключённым клиентам.

        Возвращает число соединений, в которые событие реально записалось, —
        вызывающий по нему решает, считать ли событие доставленным.
        """
        info = self._handler.describe().info
        env = make_event(event_type, data, src=Address(node=info.node, service=info.service))
        return await self.broadcast_envelope(env)

    async def broadcast_envelope(self, env: Envelope) -> int:
        """Разослать готовый конверт (в т.ч. ретрансляция события чужой ноды —
        src оригинала сохраняется). Возвращает число реальных доставок.

        Рассылка параллельная и с потолком на каждого получателя: раньше
        соединения обходились последовательно и без таймаута — один
        полумёртвый клиент с забитым Send-Q блокировал и остальных
        получателей, и await emit() у супервизора/аренды (head-of-line).
        """
        conns = [conn for conn in self._connections if conn.authenticated]
        if not conns:
            return 0
        results = await asyncio.gather(*(self._send_bounded(conn, env) for conn in conns))
        return sum(results)

    async def _send_bounded(self, conn: _Connection, env: Envelope) -> bool:
        """Записать конверт с потолком SEND_TIMEOUT_S; не уложился — клиент
        полумёртв, его соединение закрывается, чтобы не копить такие же
        зависшие записи дальше."""
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
            # Соединение принято ровно между закрытием слушателя и обходом
            # списка в stop(): обслуживать его уже некому, а брошенный
            # обработчик держал бы `wait_closed()` в заложниках.
            writer.close()
            return
        # trusted=True у unix-слушателя (права файла); на TCP доверие даёт auth.
        conn = _Connection(writer, authenticated=trusted)
        if not trusted:
            # Принятой стороне keepalive нужен так же, как исходящей: сосед,
            # умерший без FIN, иначе висит тут до tcp_retries2 (~15 минут)
            # или вечно — см. proto/tcpopts.py.
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
                        # До auth соединению не верим и ждать его вечно не
                        # готовы: молчащий клиент держал бы слот бессрочно.
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
                    break  # EOF — клиент отключился
                if line.strip() == b"":
                    continue
                # Каждый запрос — своя задача: медленный форвард к удалённой
                # ноде не блокирует остальные запросы этого соединения
                # (ответы матчатся клиентом по id, порядок не важен).
                task = asyncio.create_task(self._handle_line(conn, line))
                self._request_tasks.add(task)
                task.add_done_callback(self._request_tasks.discard)
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
                    response = None
                    if self._router is not None:
                        # Чужой адресат — маршрутизатор вернёт готовый ответ.
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
            # Тот же потолок, что у broadcast: полумёртвый клиент не должен
            # бесконечно держать задачу запроса на своём drain().
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
        if conn.authenticated:  # unix или повторный auth — токен не проверяем
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
        """Сосед назвался в auth: если это его НОВЫЙ запуск — закрыть прошлые
        (мёртвые) соединения и сообщить наверх, что линк к нему пора
        переустановить.

        Живая находка 2026-07-28: после трёх перезагрузок winpc на alfred
        висело ЧЕТЫРЕ входящих ESTAB от неё — сервер их не закрывал, потому
        что обрыв без FIN/RST для него неотличим от простоя.

        Реагируем именно на СМЕНУ инкарнации, а не на факт подключения
        (живая находка того же дня, прод): первая версия дёргала колбэк на
        каждый auth — alfred рвал линк к winpc, переподключался, его auth
        заставлял winpc порвать свой линк к alfred, и так по кругу каждые
        10 с. Инкарнация процесса при переподключении не меняется, при
        перезапуске — меняется.
        """
        node = request.payload.get("node")
        if not isinstance(node, str) or not node:
            return
        conn.node = node
        incarnation = request.payload.get("incarnation")
        if not isinstance(incarnation, str):
            incarnation = ""
        previous = self._peer_incarnations.get(node)
        self._peer_incarnations[node] = incarnation
        # Первое соединение с этим узлом за нашу жизнь: нам не с чем
        # сравнивать и нечего инвалидировать.
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


def decode_request(line: bytes) -> Envelope:
    """Декодировать строку и убедиться, что это запрос."""
    env = decode(line)
    if env.type not in REQUEST_TYPES:
        raise ProtoError(ERR_UNKNOWN_TYPE, f"ожидался запрос, пришёл {env.type}")
    return env
