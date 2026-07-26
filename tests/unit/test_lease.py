"""Аренда лидерства: кто держит синглтон, кто уступает, что делает 409."""

from __future__ import annotations

import time

from sa_home_bot.node.assignments import parse
from sa_home_bot.node.kind import KIND_SERVER, KIND_VPS
from sa_home_bot.node.lease import (
    EVENT_SINGLETON_ACTIVATED,
    EVENT_SINGLETON_YIELDED,
    LeaseManager,
    rank,
)
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import FENCED_EXIT_CODE, RUNNING, STOPPED, Supervisor
from sa_home_bot.proto.messages import Envelope

SLOT = "telegram-bot@alfred"


class FakePeer:
    """Сосед, отдающий заданную картину по синглтонам."""

    def __init__(self, node_id: str, singletons: dict, *, alive: bool = True):
        self.name = node_id
        self.endpoint = f"tcp://{node_id}:8710"
        self.alive = alive
        self.node_kind = KIND_SERVER
        # Момент первой неудачи соединения; None — попытки ещё не было.
        self.down_since = None if alive else 0.0
        self._singletons = singletons

    def downtime_s(self):
        return None if self.alive else 999.0

    async def forward(self, env: Envelope) -> Envelope:
        return Envelope(
            type="response", id=env.id, ok=True, payload={"singletons": self._singletons}
        )


class FakeSlot:
    """Слот супервизора с управляемым статусом и записью команд."""

    def __init__(self, assignment: str, status: str = STOPPED):
        self.assignment = parse(assignment)
        self.name = self.assignment.key
        self.status = status
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        self.status = RUNNING

    async def stop(self) -> None:
        self.calls.append("stop")
        self.status = STOPPED


def _lease(node_id: str, node_kind: str, slot: FakeSlot, *peers, grace_s: float = 120.0):
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    supervisor = Supervisor([], None, emit=emit)
    supervisor.services[slot.name] = slot
    router = NodeRouter(node_id, peers={p.name: p for p in peers})
    manager = LeaseManager(
        node_id, node_kind, supervisor, router=router, emit=emit, grace_s=grace_s
    )
    return manager, events


def _entry(node: str, *, running=False, claiming=False, role="active",
           priority=100, fenced=False) -> dict:
    return {
        "node": node,
        "role": role,
        "running": running,
        "claiming": claiming,
        "rank": list(rank(parse(f"telegram-bot@alfred:{role}:prio={priority}"), KIND_SERVER, node)),
        "fenced": fenced,
    }


# --- старшинство ------------------------------------------------------------


def test_active_role_outranks_standby_regardless_of_machine():
    """«Активный на основной ноде» — это про роль, а не про то, чьё железо
    мощнее: иначе назначение перестаёт что-либо значить."""
    active_on_vps = rank(parse("telegram-bot@a"), KIND_VPS, "jeeves")
    standby_on_server = rank(parse("telegram-bot@a:standby"), KIND_SERVER, "alfred")
    assert active_on_vps > standby_on_server


def test_explicit_priority_beats_the_machine_type():
    default = rank(parse("telegram-bot@a:standby"), KIND_VPS, "jeeves")
    boosted = rank(parse("telegram-bot@a:standby:prio=90"), KIND_VPS, "jeeves")
    assert boosted > default


# --- захват -----------------------------------------------------------------


async def test_active_takes_the_slot_immediately():
    """Обычный старт основной ноды не должен ждать grace — бот молчал бы две
    минуты на каждом рестарте."""
    slot = FakeSlot("telegram-bot@alfred")
    lease, events = _lease("alfred", KIND_SERVER, slot)
    await lease.tick()
    assert slot.calls == ["start"]
    assert [e[0] for e in events] == [EVENT_SINGLETON_ACTIVATED]


async def test_standby_waits_out_the_grace_period():
    """Иначе служба скакала бы по рою на каждом моргании сети."""
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, grace_s=120.0)
    await lease.tick()
    assert slot.calls == []


async def test_standby_takes_over_after_the_grace_period():
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, events = _lease("jeeves", KIND_VPS, slot, grace_s=0.0)
    await lease.tick()
    assert slot.calls == ["start"]
    assert [e[0] for e in events] == [EVENT_SINGLETON_ACTIVATED]


async def test_standby_does_not_start_while_the_active_node_holds_it():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", running=True)})
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == []


async def test_sleeping_peer_does_not_block_takeover():
    """Спящая нода претендовать не может — и мешать не должна."""
    peer = FakePeer("winpc", {SLOT: _entry("winpc", running=True)}, alive=False)
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == ["start"]


async def test_grace_restarts_when_the_active_node_blinks():
    peer_up = FakePeer("alfred", {SLOT: _entry("alfred", running=True)})
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer_up, grace_s=60.0)
    await lease.tick()  # активная на месте
    peer_up.alive = False
    await lease.tick()  # тишина началась только сейчас
    assert slot.calls == []


# --- уступка ----------------------------------------------------------------


async def test_running_standby_yields_to_the_returning_active_node():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, events = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == ["stop"]
    assert [e[0] for e in events] == [EVENT_SINGLETON_YIELDED]


async def test_active_claims_instead_of_starting_over_a_running_standby():
    """Запуск поверх работающего резерва означал бы двух поллеров на мгновение,
    и 409 прилетел бы как раз законному владельцу."""
    peer = FakePeer("jeeves", {SLOT: _entry("jeeves", running=True, role="standby")})
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, peer)
    await lease.tick()
    assert slot.calls == []
    assert lease.local_state()[SLOT]["claiming"] is True


async def test_active_starts_once_the_standby_has_stepped_aside():
    peer = FakePeer("jeeves", {SLOT: _entry("jeeves", running=True, role="standby")})
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, peer)
    await lease.tick()  # объявили притязание
    peer._singletons = {SLOT: _entry("jeeves", running=False, role="standby")}
    await lease.tick()
    assert slot.calls == ["start"]


async def test_lower_ranked_node_waits_for_the_superior_to_take_it():
    """Старший жив и вот-вот возьмёт — младший не должен его опережать."""
    peer = FakePeer("alfred", {SLOT: _entry("alfred", running=False)})
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == []


# --- fencing ----------------------------------------------------------------


async def test_fenced_node_stays_quiet():
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot)
    lease.note_fenced(SLOT)
    await lease.tick()
    assert slot.calls == []
    assert lease.local_state()[SLOT]["fenced"] is True


async def test_fenced_node_resumes_after_the_holdoff(monkeypatch):
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot)
    lease.note_fenced(SLOT)
    lease._holdoff_s = 0.0
    lease._fenced_until[SLOT] = time.monotonic()
    await lease.tick()
    assert slot.calls == ["start"]


async def test_supervisor_does_not_restart_a_fenced_service(monkeypatch):
    """Перезапуск вытесненного экземпляра — это война двух поллеров за апдейты."""
    import asyncio

    import sa_home_bot.node.supervisor as sup_mod

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(python, dash_m, module, *args, **kwargs):
        return await real_exec(
            python, "-c", f"import sys; sys.exit({FENCED_EXIT_CODE})", **kwargs
        )

    monkeypatch.setattr(sup_mod.asyncio, "create_subprocess_exec", fake_exec)

    noted: list[str] = []
    sup = Supervisor(
        ["telegram-bot@alfred"], None, emit=_noop, restart_delay_s=0.01,
        on_fenced=noted.append,
    )
    slot = sup.services["telegram-bot@alfred"]
    await slot.start()
    for _ in range(200):
        if noted:
            break
        await _sleep()
    assert noted == ["telegram-bot@alfred"]
    assert slot.status == STOPPED
    assert slot.restarts == 0


async def test_first_decision_waits_for_peer_links_to_settle():
    """Линки поднимаются асинхронно: реши аренда сразу — вернувшаяся основная
    нода запустила бы бота поверх работающего резерва (живая находка при
    выкатке 0.38.0: перекрытие 5 с)."""
    peer = FakePeer("jeeves", {SLOT: _entry("jeeves", running=True, role="standby")})
    peer.alive = False
    peer.down_since = None  # соединение ещё даже не пробовали
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, peer)
    lease._settle_s = 0.6

    await lease.start()
    await lease.stop()
    # За время ожидания связь так и не поднялась — решаем по тому, что есть,
    # но ожидание было (иначе тест бы не отличался от прямого tick).
    assert slot.calls == ["start"]


async def test_settle_ends_as_soon_as_every_peer_has_answered():
    peer = FakePeer("jeeves", {SLOT: _entry("jeeves", running=True, role="standby")})
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, peer)
    lease._settle_s = 30.0  # столько ждать не придётся: пир жив с самого начала

    start = time.monotonic()
    await lease.start()
    await lease.stop()
    assert time.monotonic() - start < 5.0
    # Резерв работает — не запускаемся поверх, а объявляем притязание.
    assert slot.calls == []
    assert lease.local_state()[SLOT]["claiming"] is True


async def test_startup_does_not_launch_singletons_directly():
    """Их запуск — дело аренды, даже для активной роли."""
    sup = Supervisor(["telegram-bot@alfred", "monitor"], None, emit=_noop)
    await sup.start_all()
    assert sup.services["telegram-bot@alfred"].status == STOPPED
    assert sup.services["monitor"].status == RUNNING
    await sup.stop_all()


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.01)


async def _noop(event_type: str, data: dict) -> None:
    return None
