from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from sa_home_bot.node.assignments import ROLE_ACTIVE, Assignment
from sa_home_bot.node.kind import traits_for
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import RUNNING, EventEmitter, Supervisor
from sa_home_bot.proto.addresses import NODE_SERVICE
from sa_home_bot.proto.messages import MSG_GET_STATE, Address, ProtoError, make_request
from sa_home_bot.services import registry
from sa_home_bot.utils.lifespan import cancel_task

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 10.0
FAILOVER_GRACE_S = 120.0
FENCED_HOLDOFF_S = 300.0
PEER_TIMEOUT_S = 5.0
STARTUP_SETTLE_S = 15.0

SUPERIOR_READY_TIMEOUT_S = 45.0

STUCK_WARN_S = 600.0

EVENT_SINGLETON_ACTIVATED = "singleton_activated"
EVENT_SINGLETON_YIELDED = "singleton_yielded"


_ROLE_RANK = {ROLE_ACTIVE: 1}


def rank(assignment: Assignment, node_kind: str, node_id: str) -> tuple[int, int, str]:
    priority = (
        assignment.priority
        if assignment.priority is not None
        else traits_for(node_kind).lease_priority
    )
    return (_ROLE_RANK.get(assignment.role, 0), priority, node_id)


def _outranks(a: tuple[int, int, str], b: tuple[int, int, str]) -> bool:
    if a[:2] != b[:2]:
        return a[:2] > b[:2]
    return a[2] < b[2]


class LeaseManager:
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
        ready_timeout_s: float = SUPERIOR_READY_TIMEOUT_S,
        stuck_warn_s: float = STUCK_WARN_S,
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
        self._ready_timeout_s = ready_timeout_s
        self._stuck_warn_s = stuck_warn_s
        self._claiming: set[str] = set()
        self._silent_since: dict[str, float] = {}
        self._fenced_until: dict[str, float] = {}
        self._ready: dict[str, bool] = {}
        self._yielded_since: dict[str, float] = {}
        self._not_running_since: dict[str, float] = {}
        self._stuck_warned: set[str] = set()
        self._task: asyncio.Task | None = None


    def _slots(self) -> list:
        return [
            svc
            for svc in self._supervisor.services.values()
            if (spec := registry.spec(svc.assignment.service)) is not None and spec.singleton
        ]

    def local_state(self) -> dict[str, dict]:
        now = time.monotonic()
        state = {}
        for slot in self._slots():
            state[slot.name] = {
                "role": slot.assignment.role,
                "running": slot.status == RUNNING,
                "claiming": slot.name in self._claiming,
                "ready": self._ready.get(slot.name, False),
                "rank": list(rank(slot.assignment, self._node_kind, self._node_id)),
                "fenced": self._fenced_until.get(slot.name, 0.0) > now,
            }
        return state

    def set_ready(self, slot_name: str, ready: bool) -> None:
        self._ready[slot_name] = ready


    async def _peer_singletons(self) -> dict[str, list[dict]]:
        by_slot: dict[str, list[dict]] = {}
        if self._router is None:
            return by_slot
        for peer_id, link in list(self._router.peers.items()):
            if not link.alive:
                continue
            request = make_request(
                MSG_GET_STATE,
                src=Address(node=self._node_id, service=NODE_SERVICE),
                dst=Address(node=peer_id, service=NODE_SERVICE),
                timeout_s=PEER_TIMEOUT_S,
            )
            try:
                response = await asyncio.wait_for(link.forward(request), PEER_TIMEOUT_S)
            except (ProtoError, TimeoutError, ConnectionError, OSError):
                continue
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
            self._not_running_since.pop(slot.name, None)
            self._stuck_warned.discard(slot.name)
            if superior_holds:
                if not slot.process_alive:
                    log.warning(
                        "Аренда %s: старшая нода вернулась, но процесс уже "
                        "упал сам — оставляю обычный перезапуск супервизора "
                        "разбираться",
                        slot.name,
                    )
                    return
                log.info(
                    "Аренда %s: старшая нода вернулась — уступаю", slot.name
                )
                await slot.stop()
                await self._emit(
                    EVENT_SINGLETON_YIELDED,
                    {"slot": slot.name, "node": self._node_id},
                )
                self._yielded_since[slot.name] = now
            return

        if self._fenced_until.get(slot.name, 0.0) > now:
            return

        since_stuck = self._not_running_since.setdefault(slot.name, now)
        if now - since_stuck >= self._stuck_warn_s and slot.name not in self._stuck_warned:
            self._stuck_warned.add(slot.name)
            log.warning(
                "Аренда %s: слот не работает уже %.0fс без перезапуска "
                "(superior_holds=%s, claiming=%s, ready=%s) — проверьте "
                "сеть/состояние процесса на нодах, держащих эту службу",
                slot.name, now - since_stuck, superior_holds,
                slot.name in self._claiming, self._ready.get(slot.name, False),
            )

        if superior_holds:
            self._claiming.discard(slot.name)
            self._silent_since.pop(slot.name, None)
            since = self._yielded_since.get(slot.name)
            if since is not None and now - since >= self._ready_timeout_s:
                superior_ready = any(
                    p.get("ready")
                    for p in superiors
                    if p.get("running") or p.get("claiming")
                )
                if not superior_ready:
                    log.warning(
                        "Аренда %s: старшая нода не подтвердила готовность за "
                        "%.0f с — забираю слот обратно",
                        slot.name, self._ready_timeout_s,
                    )
                    self._yielded_since.pop(slot.name, None)
                    await slot.start()
                    await self._emit(
                        EVENT_SINGLETON_ACTIVATED,
                        {
                            "slot": slot.name,
                            "node": self._node_id,
                            "reason": "superior_ready_timeout",
                        },
                    )
            return

        self._yielded_since.pop(slot.name, None)

        running_inferior = [p for p in inferiors if p.get("running")]
        if running_inferior:
            if slot.name not in self._claiming:
                log.info(
                    "Аренда %s: службу держит %s, объявляю притязание",
                    slot.name, running_inferior[0].get("node"),
                )
            self._claiming.add(slot.name)
            self._silent_since.pop(slot.name, None)
            return

        if superiors:
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


    def note_fenced(self, slot_name: str) -> None:
        self._fenced_until[slot_name] = time.monotonic() + self._holdoff_s
        self._claiming.discard(slot_name)
        self._silent_since.pop(slot_name, None)
        log.warning(
            "Аренда %s: службу вытеснил другой экземпляр (409) — молчу %.0f с",
            slot_name, self._holdoff_s,
        )


    async def _settle(self) -> None:
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
        await cancel_task(self._task)
        self._task = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval_s)
            try:
                await self.tick()
            except Exception:
                log.exception("Аренда: ошибка цикла")
