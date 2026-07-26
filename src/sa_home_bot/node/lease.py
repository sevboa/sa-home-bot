"""Аренда лидерства: кто из нод держит службу-синглтон прямо сейчас.

У telegram-бота может быть ровно один активный поллер на токен — второй
получает от Telegram 409 Conflict. Значит, из всех нод, которым назначен один
и тот же инстанс бота, работать должна ровно одна, и при её пропаже её место
должна занять другая, а при возвращении — вернуть.

Консенсуса тут нет и не нужно: рой из нескольких нод, часть которых штатно
спит, кворум набирает редко. Вместо голосования — **порядок старшинства**,
одинаково вычисляемый всеми:

1. роль назначения: активный старше резервного (это и есть «основная нода»);
2. приоритет — явный ``prio=N`` или выведенный из типа машины (`node/kind.py`);
3. имя ноды — лишь бы разрешить полное равенство детерминированно.

Три правила, из которых складывается поведение:

- **Уступает младший.** Увидев работающего старшего, младший останавливается.
- **Старший не врывается, а просит.** Вернувшаяся основная нода не запускает
  службу поверх работающего резерва — она объявляет притязание (``claiming``)
  и ждёт, пока резерв остановится. Иначе оба поллера на мгновение окажутся
  живыми одновременно, и 409 прилетит как раз тому, кто прав.
- **Резерв ждёт подтверждения тишины.** Пропажа старшего должна продержаться
  ``failover_grace_s``, иначе рой будет перебрасывать службу на каждом
  моргании сети.

Разрыв связи (обе стороны считают себя единственными) этой схемой не
разрешается принципиально — его ловит внешний арбитр: Telegram отдаёт 409
второму поллеру, служба выходит с ``FENCED_EXIT_CODE``, и нода уходит в
hold-off вместо бесконечного цикла перезапусков.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from sa_home_bot.node.assignments import ROLE_ACTIVE, Assignment
from sa_home_bot.node.kind import traits_for
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import RUNNING, EventEmitter, Supervisor
from sa_home_bot.proto.messages import MSG_GET_STATE, Address, ProtoError, make_request
from sa_home_bot.services import registry

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 10.0
FAILOVER_GRACE_S = 120.0
FENCED_HOLDOFF_S = 300.0
PEER_TIMEOUT_S = 5.0
# Сколько ждать связей с соседями перед самым первым решением аренды: пока
# линки не поднялись, рой выглядит пустым, и служба досталась бы тому, кто
# просто быстрее стартовал. Секунды здесь дешевле перекрытия двух поллеров.
STARTUP_SETTLE_S = 15.0

EVENT_SINGLETON_ACTIVATED = "singleton_activated"
EVENT_SINGLETON_YIELDED = "singleton_yielded"

NODE_SERVICE = "node"

_ROLE_RANK = {ROLE_ACTIVE: 1}


def rank(assignment: Assignment, node_kind: str, node_id: str) -> tuple[int, int, str]:
    """Старшинство ноды на эту службу. Больше — старше.

    Имя ноды входит с обратным знаком по смыслу (сравнение кортежей ниже
    инвертирует его), чтобы полное равенство разрешалось одинаково у всех.
    """
    priority = (
        assignment.priority
        if assignment.priority is not None
        else traits_for(node_kind).lease_priority
    )
    return (_ROLE_RANK.get(assignment.role, 0), priority, node_id)


def _outranks(a: tuple[int, int, str], b: tuple[int, int, str]) -> bool:
    """Старше ли a, чем b. При равных роли и приоритете старше меньшее имя."""
    if a[:2] != b[:2]:
        return a[:2] > b[:2]
    return a[2] < b[2]


class LeaseManager:
    """Решает, держать ли этой ноде свои службы-синглтоны."""

    def __init__(
        self,
        node_id: str,
        node_kind: str,
        supervisor: Supervisor,
        *,
        router: NodeRouter | None,
        emit: EventEmitter,
        grace_s: float = FAILOVER_GRACE_S,
        holdoff_s: float = FENCED_HOLDOFF_S,
        poll_interval_s: float = POLL_INTERVAL_S,
        settle_s: float = STARTUP_SETTLE_S,
    ) -> None:
        self._settle_s = settle_s
        self._node_id = node_id
        self._node_kind = node_kind
        self._supervisor = supervisor
        self._router = router
        self._emit = emit
        self._grace_s = grace_s
        self._holdoff_s = holdoff_s
        self._poll_interval_s = poll_interval_s
        # Мы объявили притязание на слот и ждём, пока младший уступит.
        self._claiming: set[str] = set()
        # С какого момента слот никем не занят (monotonic) — база отсчёта grace.
        self._silent_since: dict[str, float] = {}
        # До какого момента слот трогать нельзя после 409 (monotonic).
        self._fenced_until: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    # --- наши слоты ---------------------------------------------------------

    def _slots(self) -> list:
        return [
            svc
            for svc in self._supervisor.services.values()
            if (spec := registry.spec(svc.assignment.service)) is not None and spec.singleton
        ]

    def local_state(self) -> dict[str, dict]:
        """Что нода сообщает рою о своих синглтонах — на этом строят решение
        все остальные, поэтому картина должна быть честной, включая намерение
        (``claiming``) и вынужденное молчание после 409 (``fenced``)."""
        now = time.monotonic()
        state = {}
        for slot in self._slots():
            state[slot.name] = {
                "role": slot.assignment.role,
                "running": slot.status == RUNNING,
                "claiming": slot.name in self._claiming,
                "rank": list(rank(slot.assignment, self._node_kind, self._node_id)),
                "fenced": self._fenced_until.get(slot.name, 0.0) > now,
            }
        return state

    # --- взгляд на соседей --------------------------------------------------

    async def _peer_singletons(self) -> dict[str, list[dict]]:
        """Что о наших слотах говорят живые соседи: ключ слота → их записи."""
        by_slot: dict[str, list[dict]] = {}
        if self._router is None:
            return by_slot
        for peer_id, link in list(self._router.peers.items()):
            if not link.alive:
                continue  # спящая нода претендовать не может — и не мешает
            request = make_request(
                MSG_GET_STATE,
                src=Address(node=self._node_id, service=NODE_SERVICE),
                dst=Address(node=peer_id, service=NODE_SERVICE),
                timeout_s=PEER_TIMEOUT_S,
            )
            try:
                response = await asyncio.wait_for(link.forward(request), PEER_TIMEOUT_S)
            except (ProtoError, TimeoutError, ConnectionError, OSError):
                continue  # не ответил — считаем, что не претендует
            if not response.ok:
                continue
            for key, entry in (response.payload.get("singletons") or {}).items():
                record = dict(entry)
                record["node"] = peer_id
                by_slot.setdefault(key, []).append(record)
        return by_slot

    @staticmethod
    def _rank_of(record: dict) -> tuple[int, int, str]:
        raw = record.get("rank") or [0, 0, record.get("node", "")]
        return (int(raw[0]), int(raw[1]), str(raw[2]))

    # --- решение ------------------------------------------------------------

    async def tick(self) -> None:
        slots = self._slots()
        if not slots:
            return
        peers = await self._peer_singletons()
        for slot in slots:
            await self._decide(slot, peers.get(slot.name, []))

    async def _decide(self, slot, peers: list[dict]) -> None:
        now = time.monotonic()
        mine = rank(slot.assignment, self._node_kind, self._node_id)
        superiors = [p for p in peers if _outranks(self._rank_of(p), mine)]
        inferiors = [p for p in peers if _outranks(mine, self._rank_of(p))]
        superior_holds = any(p.get("running") or p.get("claiming") for p in superiors)

        if slot.status == RUNNING:
            self._claiming.discard(slot.name)
            if superior_holds:
                log.info(
                    "Аренда %s: старшая нода вернулась — уступаю", slot.name
                )
                await slot.stop()
                await self._emit(
                    EVENT_SINGLETON_YIELDED,
                    {"slot": slot.name, "node": self._node_id},
                )
            return

        if self._fenced_until.get(slot.name, 0.0) > now:
            return  # 409 уже был: лезть снова — значит мешать тому, кто прав

        if superior_holds:
            self._claiming.discard(slot.name)
            self._silent_since.pop(slot.name, None)
            return

        running_inferior = [p for p in inferiors if p.get("running")]
        if running_inferior:
            # Служба у младшего. Не запускаемся поверх — объявляем притязание
            # и ждём, пока он остановится: иначе 409 прилетит нам же.
            if slot.name not in self._claiming:
                log.info(
                    "Аренда %s: службу держит %s, объявляю притязание",
                    slot.name, running_inferior[0].get("node"),
                )
            self._claiming.add(slot.name)
            self._silent_since.pop(slot.name, None)
            return

        if superiors:
            # Старший жив, но пока не взял — его право, ждём его.
            self._silent_since.pop(slot.name, None)
            return

        grace = 0.0 if slot.assignment.role == ROLE_ACTIVE else self._grace_s
        since = self._silent_since.setdefault(slot.name, now)
        if now - since < grace:
            return
        log.info("Аренда %s: свободна — беру", slot.name)
        self._claiming.discard(slot.name)
        self._silent_since.pop(slot.name, None)
        await slot.start()
        await self._emit(
            EVENT_SINGLETON_ACTIVATED, {"slot": slot.name, "node": self._node_id}
        )

    # --- fencing ------------------------------------------------------------

    def note_fenced(self, slot_name: str) -> None:
        """Служба вышла с кодом «меня вытеснил другой экземпляр».

        Это внешний арбитр (Telegram) сказал то, чего рой знать не мог:
        где-то работает второй поллер. Единственно верное поведение —
        замолчать на время, а не перезапускаться в цикле.
        """
        self._fenced_until[slot_name] = time.monotonic() + self._holdoff_s
        self._claiming.discard(slot_name)
        self._silent_since.pop(slot_name, None)
        log.warning(
            "Аренда %s: службу вытеснил другой экземпляр (409) — молчу %.0f с",
            slot_name, self._holdoff_s,
        )

    # --- цикл ---------------------------------------------------------------

    async def _settle(self) -> None:
        """Дать связям с соседями подняться до первого решения.

        Линки поднимаются асинхронно, и в первую же секунду после старта ноды
        все пиры выглядят недоступными. Решать по такой картине нельзя:
        вернувшаяся основная нода сочла бы службу свободной и запустила её
        поверх работающего резерва — ровно то перекрытие, которого избегает
        механизм притязаний. Ждём, пока каждый пир либо ответит, либо получит
        свою попытку соединения (``down_since`` ставится при первой неудаче).
        """
        if self._router is None or not self._router.peers:
            return
        deadline = time.monotonic() + self._settle_s
        while time.monotonic() < deadline:
            unknown = [
                link
                for link in self._router.peers.values()
                if not link.alive and link.down_since is None
            ]
            if not unknown:
                return
            await asyncio.sleep(0.5)
        log.info(
            "Аренда: не все соседи ответили за %.0f с — решаю по тому, что есть",
            self._settle_s,
        )

    async def start(self) -> None:
        await self._settle()
        with contextlib.suppress(Exception):
            await self.tick()
        self._task = asyncio.create_task(self._run(), name="lease-manager")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.tick()
            except Exception:  # аренда не должна ронять ноду
                log.exception("Аренда: ошибка цикла")
