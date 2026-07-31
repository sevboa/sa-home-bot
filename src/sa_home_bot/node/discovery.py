"""Маячок роя: ноды находят друг друга в локальной сети сами (этап 31).

Запрос уходит broadcast'ом на UDP-порт роя, ответ возвращается unicast'ом
отправителю. Всё, что маячок даёт, — это КАНДИДАТЫ адресов: рабочий канал
по-прежнему поднимается обычным TCP с auth по ``[swarm].token``
(PROTOCOL.md, «Транспорт»). Поэтому и доверия к содержимому датаграммы не
требуется: подсунутый адрес просто не пройдёт аутентификацию.

Два решения, которые стоит держать в голове при чтении:

- **Найденный сосед идёт в обычный ``join``.** Маячок не собирает линки и не
  пишет состояние сам — он зовёт ``NodeService.join()``, тот же путь, что и
  ``nodectl join``. Оттуда бесплатно достаётся всё остальное: полный граф
  пиров за один round-trip, персистентность и событие ``node_joined``,
  которым рой достраивает сетку (node/app.py::_relay_peer_event). Маячок
  заменяет человека, вводившего команду, а не механизм.
- **Порт обязан быть неотличим от закрытого.** Пакет с неверной подписью,
  чужой версией или мусором вместо JSON дропается молча: без ответа и без
  записи в лог на INFO. Иначе сканирование сети находило бы ноду по одному
  только факту реакции.

Ограничение по природе broadcast'а: он живёт в локальном сегменте и через
tailscale не проходит. Ноды вне локалки добавляются статикой (``[[swarm.nodes]]``)
или разовым ``[swarm].join`` — как и раньше.
"""

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

log = logging.getLogger(__name__)

# Версия формата датаграммы — своя, не версия протокола v0: это отдельный
# кадр в отдельном транспорте, и меняться они будут независимо.
DISCOVERY_V = 0

# Потолок на датаграмму. Реальный кадр — сотни байт (id, пара адресов,
# подпись); всё, что заметно длиннее, — не наш собеседник.
MAX_DATAGRAM_BYTES = 1024

TYPE_QUERY = "q"
TYPE_REPLY = "r"

# Сколько своих последних nonce помнить, чтобы принять ответ. Больше одного
# нужно, потому что ответ соседа может прийти уже после того, как мы послали
# следующий запрос (спящая машина отвечает не мгновенно).
REMEMBERED_NONCES = 4

# Потолок на адреса из одного ответа: сосед объявляет два-три (LAN, оверлей),
# и раздувать линк чужим списком незачем (у PeerLink свой MAX_ENDPOINTS).
MAX_REPLY_ENDPOINTS = 8

JoinCallable = Callable[[str], Awaitable[dict]]


def _canonical(body: dict) -> bytes:
    """Байты для подписи: тело без ``sig``, детерминированно сериализованное."""
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
    """Датаграмма → тело сообщения; ``None`` — дропнуть молча.

    Проверяется всё, что может отличаться у чужого: длина, разбор JSON,
    версия, тип, наличие имени и nonce и, главное, подпись общим токеном роя.
    """
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
    """Адреса из ответа, по которым имеет смысл пробовать соседа.

    Loopback отсекаем по той же причине, что и в ``advertisable``: по нему мы
    попадём не к соседу, а к себе. Нераспознанные строки — просто мусор.
    """
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
    """Как скоро слать следующий запрос.

    Учащаемся, пока искать есть кого: пиров нет вовсе (новая нода) или хоть
    один недоступен (уснул, переехал на другой адрес). Когда весь рой на
    связи, маячок почти молчит — фоновый запрос нужен лишь для того, чтобы
    новичок нашёл нас, а не только мы его.
    """
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
        # ICMP «порт недоступен» на broadcast-рассылку — обычное дело:
        # на том адресе просто никого нет. Ронять маячок из-за этого нельзя.
        log.debug("discovery: ошибка UDP: %s", exc)


class SwarmDiscovery:
    """Слушает маячок роя и сам зовёт: один UDP-сокет на приём и отправку."""

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
        # ``port`` — только для тестов: 0 просит порт у ОС. В конфиге такого
        # значения нет и быть не должно (соседям надо знать, куда стучаться),
        # поэтому это отдельный параметр, а не поле DiscoveryConfig — тем же
        # приёмом тесты поднимают ProtoServer на TcpEndpoint(host, 0).
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
        # Стучимся в ПОРТ РОЯ, а не в свой собственный: у соседа маячок слушает
        # cfg.port, и совпадение с нашим локальным портом — случайность
        # (она же скрыла бы ошибку в проде, где оба равны 32167).
        # Сети без вычисленного broadcast'а (или машина, где psutil его не
        # отдал) — не повод молчать: ограниченный broadcast доедет до своего
        # сегмента и так.
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
            # Занятый порт для маячка — не повод не работать: рой живёт на
            # TCP, а discovery лишь избавляет от ручного join.
            log.warning(
                "discovery: не занять UDP-порт %s (%s) — работаем без маячка", self._port, exc
            )
            return
        self._transport = transport
        sock = transport.get_extra_info("sockname")
        if sock:  # порт 0 в тестах — узнать реальный
            self._port = sock[1]
        self._task = asyncio.create_task(self._run(), name="swarm-discovery")
        log.info("discovery: маячок роя на UDP :%s", self._port)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
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
        """Что показать в get_state ноды: почему сосед не нашёлся, иначе
        выясняется только по логам."""
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
            return  # свой же broadcast вернулся к нам
        if body["t"] == TYPE_QUERY:
            self._reply_to(body["nonce"], addr)
            return
        if body["nonce"] not in self._nonces:
            return  # ответ не на наш запрос — replay или чужой разговор
        self._replies_seen += 1
        self._spawn(self._handle_reply(node, body))

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._jobs.add(task)
        task.add_done_callback(self._jobs.discard)

    def _reply_to(self, nonce: str, addr: tuple) -> None:
        endpoints = self._advertise()
        if not endpoints or self._transport is None:
            return  # нечего объявить — и отвечать нечем
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
            return  # на связи — адреса и так приезжают в hello
        fresh = [e for e in endpoints if e not in link.endpoints]
        if not fresh:
            return
        # Сосед переехал (сменился DHCP-адрес) — выученный путь ведёт в
        # пустоту, а линк добросовестно долбится именно в него. Даём ему
        # новый адрес и будим сразу, не дожидаясь очередного цикла проб.
        link.learn_endpoints(endpoints)
        link.reconnect_now(f"discovery: новый адрес {endpoints[0]}")
        log.info("discovery: у пира %s новый адрес %s", node, endpoints[0])

    async def _join_found(self, node: str, endpoints: Sequence[str]) -> None:
        if node in self._joining:
            return  # уже присоединяемся — ответы приходят пачкой
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
        """Один запрос в эфир."""
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
                # Интерфейс мог уехать между вычислением цели и отправкой.
                log.debug("discovery: не отправить запрос на %s:%s (%s)", host, port, exc)
        self._last_query_at = time.time()

    async def _run(self) -> None:
        self.query_once()  # первый залп сразу: новая нода ищет рой немедленно
        while True:
            await asyncio.sleep(next_interval(self._router.peers.values(), self._cfg))
            try:
                self.query_once()
            except Exception:  # маячок не должен ронять ноду
                log.exception("discovery: ошибка рассылки запроса")
