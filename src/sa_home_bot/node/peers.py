"""Межнодовая маршрутизация: линки к пирам/локальным службам + маршрутизатор.

Правило «спроси любого» (ARCHITECTURE §11 п. 2): запрос с чужим ``dst``
нода пересылает сама — к удалённой ноде по ``dst.node`` (статический список
``[[swarm.nodes]]``) или к своей локальной службе по ``dst.service``.
Клиент не знает и не должен знать, кто исполнил.

Недоступность — честная и быстрая (правило п. 4): неизвестный адресат →
``unknown_dst``, известный, но без соединения → ``unavailable``. События
пиров ретранслируются клиентам ноды с сохранением исходного ``src``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from sa_home_bot.proto.client import EventCallback, ProtoClient
from sa_home_bot.proto.endpoints import Endpoint
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Envelope,
    ProtoError,
)

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0

# Служба самого сервиса ноды: запросы к ней (и без dst) обрабатываются локально.
NODE_SERVICE = "node"


class PeerLink:
    """Постоянный линк к endpoint'у (удалённая нода или локальная служба).

    Фоновая задача держит соединение и переподключается после обрыва;
    ``forward`` пересылает конверт как есть. Нет соединения — быстрый
    ``unavailable``, а не зависание.
    """

    def __init__(
        self,
        name: str,
        endpoint: str | Endpoint,
        *,
        token: str = "",
        on_event: EventCallback | None = None,
        reconnect_delay: float = RECONNECT_DELAY_S,
    ) -> None:
        self.name = name
        self.endpoint = endpoint
        self._token = token
        self._on_event = on_event
        self._delay = reconnect_delay
        self._client: ProtoClient | None = None
        self._task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"peer-link-{self.name}")

    async def stop(self) -> None:
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
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise ProtoError(ERR_UNAVAILABLE, f"{self.name}: {exc}") from exc

    async def _run(self) -> None:
        logged_down = False
        while True:
            client = ProtoClient(self.endpoint, token=self._token, on_event=self._on_event)
            try:
                await client.connect()
                info = await client.hello()
                if info.node != self.name and info.service != self.name:
                    log.warning(
                        "PeerLink %s: на %s отвечает %s/%s — проверь конфиг",
                        self.name,
                        self.endpoint,
                        info.node,
                        info.service,
                    )
                log.info(
                    "PeerLink %s: связь установлена (%s/%s)", self.name, info.node, info.service
                )
                logged_down = False
                self._client = client
                try:
                    await client.join()
                finally:
                    self._client = None
                log.warning("PeerLink %s: связь потеряна, переподключение...", self.name)
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                if not logged_down:
                    log.warning(
                        "PeerLink %s недоступен (%s) — переподключение каждые %.0f с",
                        self.name,
                        exc,
                        self._delay,
                    )
                    logged_down = True
            finally:
                self._client = None
                await client.close()
            await asyncio.sleep(self._delay)


class NodeRouter:
    """Маршрутизатор запросов сервиса ноды (хук ``router`` у ProtoServer).

    ``route`` возвращает готовый конверт-ответ, если запрос переслан,
    или None — запрос локальный (обработает NodeService).
    """

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
        # Чужая нода — переслать её сервису ноды целиком (он сам довезёт
        # до своей службы по dst.service).
        if dst.node is not None and dst.node != self.node_id:
            peer = self.peers.get(dst.node)
            if peer is None:
                known = ", ".join(self.peers) or "нет пиров"
                raise ProtoError(ERR_UNKNOWN_DST, f"неизвестная нода: {dst.node} (есть: {known})")
            return await peer.forward(request)
        # Своя нода: запрос к локальной службе — проксировать по dst.service.
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
        """Presence пиров для get_state ноды (/nodes, nodectl status)."""
        return [
            {"id": link.name, "endpoint": str(link.endpoint), "alive": link.alive}
            for link in self.peers.values()
        ]

    async def add_local_service(self, name: str, link: PeerLink) -> None:
        """Добавить проксируемую локальную службу в рантайме (assign, этап 17)."""
        self.local_services[name] = link
        await link.start()

    async def remove_local_service(self, name: str) -> None:
        link = self.local_services.pop(name, None)
        if link is not None:
            await link.stop()

    async def add_peer(self, link: PeerLink) -> None:
        """Добавить пира в рантайме (join, этап 18)."""
        self.peers[link.name] = link
        await link.start()

    async def remove_peer(self, node_id: str) -> None:
        link = self.peers.pop(node_id, None)
        if link is not None:
            await link.stop()
