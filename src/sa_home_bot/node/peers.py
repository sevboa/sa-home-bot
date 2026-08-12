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
from collections.abc import Callable, Sequence
from pathlib import Path

from sa_home_bot.proto.client import EventCallback, ProtoClient
from sa_home_bot.proto.endpoints import (
    Endpoint,
    endpoint_rank,
    is_loopback,
    parse_endpoint,
)
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Address,
    Envelope,
    ProtoError,
    ServiceInfo,
)

log = logging.getLogger(__name__)

RECONNECT_DELAY_S = 5.0

# Потолок на одну попытку подключения (connect + hello). Нужен именно из-за
# перебора адресов (этап 24): у адреса, до которого нет маршрута, ОС-таймаут
# TCP-connect — десятки секунд или минуты, и линк всё это время не пробовал
# бы следующий, рабочий адрес. Локальная сеть отвечает за миллисекунды,
# tailscale — за десятки миллисекунд; пяти секунд хватает с запасом.
CONNECT_TIMEOUT_S = 5.0

# Сколько адресов помнить про одного соседа. Выученные адреса не забываются
# (сосед может вернуться по любому из них), но и копиться бесконечно им
# незачем: каждый лишний — это ещё одна неудачная попытка в цикле проб.
MAX_ENDPOINTS = 8

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

# Локальным службам (тот же хост, линк node -> своя же служба, см.
# node/app.py::make_link_factories) 4 секунд мало: CPU-тяжёлая работа внутри
# службы (например XTTS-синтез в llm/tts.py) может ненадолго не пускать её
# event loop ответить на hello(), и линк рвётся посреди уже летящего
# запроса — тот теряется вместе с соединением, хотя служба на самом деле
# жива и просто занята. Сетевым пирам такой запас не нужен: там сама суть
# heartbeat — поймать зависший процесс/обрыв сети быстро.
LOCAL_HEARTBEAT_TIMEOUT_S = 20.0

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


def _parse_all(values: Sequence[str | Path | Endpoint]) -> list[Endpoint]:
    """Разобрать адреса, пропустив битые: одна опечатка в списке не должна
    лишать линк остальных, рабочих путей."""
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
    """Подключиться и спросить «кто ты» — одной операцией под общим таймаутом."""
    await client.connect()
    return await client.hello()


def _answers_to(info: ServiceInfo, name: str) -> bool:
    """Тот ли, к кому шли: для соседа-ноды сходится имя узла, для локальной
    службы — имя службы. Регистр не важен: имя ноды — это hostname, и в
    конфиге его пишут как придётся."""
    return name.lower() in (info.node.lower(), info.service.lower())


def _order_endpoints(
    endpoints: Sequence[Endpoint], first: Endpoint | None = None
) -> list[Endpoint]:
    """Порядок проб: последний удачный → локальный путь → оверлей.

    ``sorted`` стабилен, поэтому внутри одного ранга сохраняется исходный
    порядок (конфиг важнее выученного — он написан человеком).
    """
    ordered = sorted(endpoints, key=endpoint_rank)
    if first is not None and first in ordered:
        ordered.remove(first)
        ordered.insert(0, first)
    return ordered


class PeerLink:
    """Постоянный линк к соседу (удалённая нода или локальная служба).

    Фоновая задача держит соединение и переподключается после обрыва;
    ``forward`` пересылает конверт как есть. Нет соединения — быстрый
    ``unavailable``, а не зависание.

    Адресов у соседа может быть несколько (этап 24): LAN, tailscale, и те,
    что он сам назвал в hello. Линк пробует их по очереди — последний
    удачный, затем локальные, затем оверлей (`proto/endpoints.endpoint_rank`).
    ``endpoint`` — тот адрес, по которому связь есть (или была) сейчас.
    """

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
        # Первый заданный адрес — «последний удачный» (так его кладёт нода,
        # поднимая линк из состояния): с него и начинаем перебор.
        self._candidates: list[Endpoint] = _order_endpoints(parsed, first=parsed[0])
        # Адрес, по которому связь есть/была последний раз: с него начинается
        # следующий перебор и его показывает /nodes.
        self.endpoint: Endpoint = self._candidates[0]
        # Кому сообщать выученные адреса соседа (нода сохраняет их в своём
        # состоянии — переживают рестарт, см. node/state.py).
        self._on_endpoints = on_endpoints
        self._token = token
        self._on_event = on_event
        self._delay = reconnect_delay
        # Имя СВОЕЙ ноды: едет в auth, чтобы сосед узнал нас при
        # переподключении (см. proto/server.py::_note_peer).
        self._src = Address(node=self_node) if self_node else None
        # Живая находка 2026-08-04: адрес соседа, однажды выученный
        # (learn_endpoints, "не забываются"), может через недели DHCP-churn
        # без резервации оказаться переехавшим на ДРУГУЮ машину — в том числе
        # на нас самих. `_dial` тогда стучится сам к себе, получает в ответ
        # свой же node_id и раньше принимал это за «сосед просто иначе
        # называется» (последняя очередь) — линк несколько минут ходил по
        # кругу «подключился к себе → запрос не доехал → переподключение»,
        # и PresenceWatcher на каждом витке рапортовал ложное «вернулась в
        # строй». Знать своё имя здесь — единственный способ отличить это от
        # легитимного «сосед назвался иначе, чем в конфиге».
        self._self_node = self_node.lower() if self_node else ""
        self._client: ProtoClient | None = None
        self._task: asyncio.Task | None = None
        # Чем закончилась последняя попытка перебора адресов — только для лога.
        self._last_error: Exception | None = None
        # Мы сами рвём соединение (heartbeat/reconnect_now), а не нас
        # останавливают: `ProtoClient.close()` отменяет читающую задачу, и
        # `join()` в `_run` получает CancelledError — без этого флага она
        # неотличима от stop() и убивала бы весь цикл переподключения.
        self._dropping = False
        # Линк останавливают насовсем (stop): отмену в этом состоянии нельзя
        # спутать с нашим же сбросом соединения — см. `_serve` (этап 29).
        self._stopping = False
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
        # Сосед предупредил, что гасится штатно (событие node_leaving,
        # этап 23): его недоступность — не авария даже для always_on-машины.
        # Сбрасывается, когда связь установилась заново.
        self.left: bool = False

    @property
    def alive(self) -> bool:
        return self._client is not None

    @property
    def endpoints(self) -> list[str]:
        """Все известные адреса соседа, в порядке проб."""
        return [str(ep) for ep in self._candidates]

    def learn_endpoints(self, advertised: Sequence[str]) -> None:
        """Взять адреса ЭТОГО hello как полную и единственную правду о
        соседе — не добавить к тому, что копилось раньше.

        Живая находка 2026-08-04: раньше выученное только прирастало ("не
        забывается, сосед может вернуться по любому из них") — на практике
        это значило, что стухший адрес (DHCP без резервации отдал его
        другой машине, в том числе нам самим) простаивал в списке
        кандидатов НАВСЕГДА, в надежде, что однажды "вернётся к старому".
        Сосед сам знает точнее любой нашей памяти о прошлом, где он сейчас
        слушает — берём ровно то, что он сказал ПРЯМО СЕЙЧАС, плюс адрес,
        на котором связь есть в этот момент (он не может быть стухшим по
        определению). Конфиг/состояние, с которых линк стартовал, не
        трогаем здесь — они остаются попыткой для ПЕРВОГО подключения, пока
        соседа ни разу не удавалось спросить о себе.

        Loopback отбраковывается жёстко: по ``127.0.0.1`` мы попадём не к
        соседу, а к себе — и, если у себя тот же порт, примем свою же ноду
        за него (от этого спасает ещё и сверка имени в `_dial`).
        """
        # Адрес, на котором связь есть прямо сейчас, не проходит фильтр
        # loopback: unix-сокет — заведомо валидный путь ИМЕННО когда мы уже
        # соединены по нему (в отличие от только что объявленного 127.0.0.1
        # соседом — тому верить нельзя, см. докстринг).
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
        # Флаг ДО cancel: иначе отмена, пришедшая ровно в окно между
        # `reconnect_now()` и пробуждением `_serve`, была бы неотличима от
        # нашего собственного сброса соединения — её проглотили бы, и линк
        # продолжил бы переподключаться, а `await self._task` не вернулся бы
        # никогда (этап 29; на проде это выглядело как «нода не гасится»).
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
        except (ConnectionError, OSError, TimeoutError) as exc:
            # Запрос ушёл в никуда — линк подозрителен, переустанавливаем его
            # сразу, а не ждём, пока это заметит heartbeat: иначе следующий
            # запрос повторит ту же минуту ожидания.
            self.reconnect_now(f"запрос не доехал ({exc})")
            raise ProtoError(ERR_UNAVAILABLE, f"{self.name}: {exc}") from exc

    def note_left(self) -> None:
        """Сосед объявил штатный уход (node_leaving): пометить и уронить линк
        сразу — точный детект вместо ожидания heartbeat. Цикл переподключения
        продолжает стучаться как обычно (дёшево: connection refused) — он же
        и заметит возвращение."""
        self.left = True
        self.reconnect_now("нода уходит штатно")
        # Линк мог быть уже оборван (reconnect_now тогда ничего не делает) —
        # отсчёт простоя всё равно должен начаться, иначе PresenceWatcher
        # не увидит ни ухода, ни возвращения.
        self._mark_down()

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

    async def _dial(self) -> tuple[ProtoClient, ServiceInfo, Endpoint] | None:
        """Пройти адреса соседа по порядку до первого, который ответил СВОИМ
        именем. None — не ответил ни один.

        Каждая попытка ограничена ``CONNECT_TIMEOUT_S``: без потолка адрес,
        до которого нет маршрута, съедал бы ОС-таймаут TCP-connect (десятки
        секунд), и следующий, рабочий адрес не пробовался бы вовсе.

        Ответивший чужим именем — кандидат последней очереди, а не отказ: имя
        в ``[[swarm.nodes]]`` человек пишет как ему удобно, и оно вполне
        законно может не совпадать с hostname соседа. Пока есть другие
        адреса, пробуем их; не нашлось ничего лучше — берём его и ругаемся
        в лог, как ругались до этапа 24.
        """
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
                # Синхронный abort, а не await close(): нас уже отменяют,
                # любой await здесь получит отмену повторно и не доработает.
                client.abort()
                if mismatched is not None:
                    mismatched[0].abort()
                raise
            except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
                last_error = exc
                with contextlib.suppress(Exception):
                    await client.close()
                continue
            # Не тот, к кому шли: по этому адресу либо чужая нода, либо мы
            # сами (сосед мог объявить адрес, который в НАШЕЙ сети занят
            # кем-то другим) — но, возможно, и просто другое имя в конфиге.
            if not _answers_to(info, self.name):
                # Ответил я сам — адрес соседа переехал (DHCP без резервации
                # отдал его другой машине, в том числе нам). Это не «сосед
                # иначе называется», а гарантированно мёртвый кандидат —
                # никогда не годится даже как запасной вариант, иначе линк
                # зацикливается сам на себя (см. комментарий у _self_node).
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
        """Связь установлена: держать её, пока не оборвётся.

        Своё представление о соседе (адрес, тип, WoL, список путей)
        обновляем, только если ответ ПОДТВЕРЖДЁННО от него самого — не от
        «запасного» мисматча (см. `_dial`): иначе устаревший кандидат, на
        который сейчас отвечает ДРУГОЙ реальный сосед (не мы — тот случай
        `_dial` уже отсекает целиком), приписал бы нашему линку чужие
        данные. Инцидент 2026-08-04: так в список адресов «mycraft»
        однажды попал настоящий LAN-адрес «winpc». Соединение при этом
        всё равно используется (см. ниже) — «запасной» ответ лучше, чем
        никакого, но он не должен становиться новой памятью о соседе.
        """
        confirmed = _answers_to(info, self.name)
        if confirmed:
            switched = endpoint != self.endpoint
            if switched:
                log.info("PeerLink %s: путь сменился на %s", self.name, endpoint)
            # Удачный адрес становится первым в следующем переборе — и он же
            # переживает рестарт ноды (см. on_endpoints).
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
        self.left = False  # вернулась — прошлый штатный уход исчерпан
        self._client = client
        heartbeat = asyncio.create_task(self._heartbeat(client), name=f"peer-link-hb-{self.name}")
        try:
            await client.join()
        except asyncio.CancelledError:
            # Соединение оборвали МЫ (см. `_dropping`) — это штатный путь к
            # переподключению. Отмена не наша — пробрасываем, иначе stop()
            # не остановит линк. `_stopping` старше `_dropping`: линк, который
            # гасят, не переподключается, даже если мы сами только что сбросили
            # соединение.
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
                raise  # stop(): выходим из цикла, это единственный законный путь
            except Exception:
                # Живая находка 2026-07-28: любое НЕОЖИДАННОЕ исключение здесь
                # убивало задачу линка молча (create_task никому не жалуется) —
                # линк оставался мёртвым навсегда, пока не перезапустят ноду.
                # Цикл переподключения обязан переживать что угодно.
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
                # Все известные пути к соседу (этап 24): по ним нода, узнавшая
                # о нём от другой, свяжется напрямую хоть по LAN, хоть по
                # tailscale — а не по одному адресу из чужого конфига.
                "endpoints": link.endpoints,
                "alive": link.alive,
                # Тип соседа и длительность недоступности: по ним фронтенд
                # отличает «спит, это норма» от «пропал сервер, это авария».
                "kind": link.node_kind,
                "down_s": link.downtime_s(),
                # Штатный уход (node_leaving) — фронтенд отличает «выключена
                # нарочно» от «пропала» и не пугает красной лампой.
                "left": link.left,
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
