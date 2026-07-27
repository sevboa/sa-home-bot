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
import time
import uuid

from sa_home_bot.proto.client import EventCallback, ProtoClient
from sa_home_bot.proto.endpoints import Endpoint
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Address,
    Envelope,
    ProtoError,
)

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0

# Живая находка 2026-07-28: одних ОС-таймаутов мало. TCP не замечает
# собеседника, у которого сокет жив, а процесс завис (не читает и не
# отвечает) — а именно это неотличимо от «нода работает» по состоянию
# соединения. Прикладной heartbeat ловит и это, и всё остальное: сон,
# ребут без FIN/RST, забитый Send-Q — независимо от платформы и настроек ОС.
#
# `hello` берём как пробу намеренно: он уже реализован обеими сторонами и
# ничего не меняет на той стороне — расширять протокол ради ping не нужно.
HEARTBEAT_INTERVAL_S = 5.0
HEARTBEAT_TIMEOUT_S = 4.0
# Промахов подряд до разрыва: один потерянный ответ на нагруженной ноде —
# не повод рвать рабочий линк, два подряд — уже отказ.
HEARTBEAT_MISSES = 2

# Инкарнация процесса: новая на каждый запуск ноды. Сосед по ней отличает
# «я перезагрузился» от «я просто переподключился».
#
# Живая находка 2026-07-28 (прод, через полчаса после первой версии фикса):
# без неё случился взаимный цикл — alfred рвал линк к winpc, переподключался,
# его auth заставлял winpc порвать свой линк к alfred, и так каждые 10 с по
# кругу. Реагировать надо на СМЕНУ инкарнации, а не на факт подключения.
NODE_INCARNATION = uuid.uuid4().hex

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
        self_node: str = "",
    ) -> None:
        self.name = name
        self.endpoint = endpoint
        self._token = token
        self._on_event = on_event
        self._delay = reconnect_delay
        # Имя СВОЕЙ ноды: едет в auth, чтобы сосед узнал нас при
        # переподключении (см. proto/server.py::_note_peer).
        self._src = Address(node=self_node) if self_node else None
        self._client: ProtoClient | None = None
        self._task: asyncio.Task | None = None
        # Мы сами рвём соединение (heartbeat/reconnect_now), а не нас
        # останавливают: `ProtoClient.close()` отменяет читающую задачу, и
        # `join()` в `_run` получает CancelledError — без этого флага она
        # неотличима от stop() и убивала бы весь цикл переподключения.
        self._dropping = False
        # Тип машины соседа из его hello. Держим и после обрыва: чтобы решить,
        # нормально ли, что нода пропала, нужно знать её тип именно тогда,
        # когда её уже не спросить.
        self.node_kind: str = ""
        # Ethernet-реквизиты соседа из его hello (ServiceInfo.wake). Держим и
        # после обрыва по той же причине, что и node_kind: чтобы РАЗБУДИТЬ
        # ноду, надо знать её MAC именно тогда, когда её уже не спросить.
        self.wake_info: dict[str, str] | None = None
        # Момент последней потери связи (monotonic) — от него отсчитывается
        # порог «пропала слишком надолго» для алерта о ноде, которая обязана
        # быть в сети. None — связь есть либо ещё ни разу не устанавливалась.
        self.down_since: float | None = None

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
            # Запрос ушёл в никуда — линк подозрителен, переустанавливаем его
            # сразу, а не ждём, пока это заметит heartbeat: иначе следующий
            # запрос повторит ту же минуту ожидания.
            self.reconnect_now(f"запрос не доехал ({exc})")
            raise ProtoError(ERR_UNAVAILABLE, f"{self.name}: {exc}") from exc

    def reconnect_now(self, reason: str) -> None:
        """Считать текущее соединение мёртвым и переподключиться немедленно.

        Нужно, когда о смерти линка известно РАНЬШЕ любых таймаутов — сосед
        сам постучался к нам заново (значит, он перезагрузился, а наш
        исходящий линк к нему держит труп прошлого соединения, см.
        proto/server.py). Закрытие клиента роняет `join()` в `_run`, дальше
        обычный путь переподключения.
        """
        client = self._client
        if client is None:
            return
        log.info("PeerLink %s: %s — переподключаюсь немедленно", self.name, reason)
        self._client = None
        self._dropping = True
        # abort(), а не close(): закрывает соединение ровно один владелец —
        # цикл `_run` в своём finally (см. ProtoClient.abort).
        client.abort()

    async def _heartbeat(self, client: ProtoClient) -> None:
        """Периодическая проба соединения (см. HEARTBEAT_* выше).

        ``HEARTBEAT_MISSES`` промахов подряд — закрываем клиента, чем роняем
        `join()` в `_run`; переподключение дальше идёт обычным путём.
        """
        misses = 0
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            try:
                await asyncio.wait_for(client.hello(), timeout=HEARTBEAT_TIMEOUT_S)
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
                    client.abort()  # закрывает `_run`, см. reconnect_now
                    return

    def _mark_down(self) -> None:
        """Запомнить момент потери связи — только первый, чтобы циклы
        переподключения не сбрасывали отсчёт до порога алерта."""
        if self.down_since is None:
            self.down_since = time.monotonic()

    def downtime_s(self) -> float | None:
        """Сколько секунд связи нет; None — связь есть."""
        if self.alive or self.down_since is None:
            return None
        return time.monotonic() - self.down_since

    async def _run(self) -> None:
        logged_down = False
        while True:
            client = ProtoClient(
                self.endpoint,
                token=self._token,
                on_event=self._on_event,
                src=self._src,
                incarnation=NODE_INCARNATION,
            )
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
                if info.node_kind:
                    self.node_kind = info.node_kind
                if info.wake:
                    self.wake_info = info.wake
                self.down_since = None
                self._client = client
                heartbeat = asyncio.create_task(
                    self._heartbeat(client), name=f"peer-link-hb-{self.name}"
                )
                try:
                    await client.join()
                except asyncio.CancelledError:
                    # Соединение оборвали МЫ (см. `_dropping`) — это штатный
                    # путь к переподключению. Отмена не наша — пробрасываем,
                    # иначе stop() не остановит линк.
                    if not self._dropping:
                        raise
                finally:
                    self._dropping = False
                    heartbeat.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat
                    self._client = None
                    self._mark_down()
                log.warning("PeerLink %s: связь потеряна, переподключение...", self.name)
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                self._mark_down()
                if not logged_down:
                    log.warning(
                        "PeerLink %s недоступен (%s) — переподключение каждые %.0f с",
                        self.name,
                        exc,
                        self._delay,
                    )
                    logged_down = True
            except asyncio.CancelledError:
                raise  # stop(): выходим из цикла, это единственный законный путь
            except Exception:
                # Живая находка 2026-07-28: любое НЕОЖИДАННОЕ исключение здесь
                # убивало задачу линка молча (create_task никому не жалуется) —
                # линк оставался мёртвым навсегда, пока не перезапустят ноду.
                # Цикл переподключения обязан переживать что угодно.
                self._mark_down()
                log.exception("PeerLink %s: непредвиденная ошибка, продолжаю попытки", self.name)
            finally:
                self._client = None
                # Закрытие не должно задерживать следующую попытку: сокет мог
                # остаться полумёртвым (см. ProtoClient.close).
                with contextlib.suppress(Exception):
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
            {
                "id": link.name,
                "endpoint": str(link.endpoint),
                "alive": link.alive,
                # Тип соседа и длительность недоступности: по ним фронтенд
                # отличает «спит, это норма» от «пропал сервер, это авария».
                "kind": link.node_kind,
                "down_s": link.downtime_s(),
                # Реквизиты WoL соседа, известные с последнего hello — так
                # любая служба может разбудить его, не имея собственного
                # кэша опросов (см. wake_core.resolve_wake_info).
                "wake": link.wake_info,
            }
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
