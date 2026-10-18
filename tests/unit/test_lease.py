from __future__ import annotations

import logging
import time

from sa_home_bot.node.assignments import parse
from sa_home_bot.node.events import EVENT_SINGLETON_ACTIVATED, EVENT_SINGLETON_YIELDED
from sa_home_bot.node.kind import KIND_SERVER, KIND_VPS
from sa_home_bot.node.lease import LeaseManager, rank
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import FENCED_EXIT_CODE, RUNNING, STOPPED, Supervisor
from sa_home_bot.proto.messages import Envelope

SLOT = "telegram-bot@alfred"


class FakePeer:
    def __init__(self, node_id: str, singletons: dict, *, alive: bool = True):
        self.name = node_id
        self.endpoint = f"tcp://{node_id}:8710"
        self.alive = alive
        self.node_kind = KIND_SERVER
        self.down_since = None if alive else 0.0
        self._singletons = singletons

    def downtime_s(self):
        return None if self.alive else 999.0

    async def forward(self, env: Envelope) -> Envelope:
        return Envelope(
            type="response", id=env.id, ok=True, payload={"singletons": self._singletons}
        )


class FakeSlot:
    def __init__(self, assignment: str, status: str = STOPPED, *, process_alive: bool = True):
        self.assignment = parse(assignment)
        self.name = self.assignment.key
        self.status = status
        self.process_alive = process_alive
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        self.status = RUNNING

    async def stop(self) -> None:
        self.calls.append("stop")
        self.status = STOPPED


def _lease(
    node_id: str,
    node_kind: str,
    slot: FakeSlot,
    *peers,
    grace_s: float = 120.0,
    ready_timeout_s: float = 9999.0,
    stuck_warn_s: float = 9999.0,
):
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    supervisor = Supervisor([], None, emit=emit)
    supervisor.services[slot.name] = slot
    router = NodeRouter(node_id, peers={p.name: p for p in peers})
    manager = LeaseManager(
        node_id,
        node_kind,
        supervisor,
        router=router,
        emit=emit,
        grace_s=grace_s,
        ready_timeout_s=ready_timeout_s,
        stuck_warn_s=stuck_warn_s,
    )
    return manager, events


def _entry(
    node: str,
    *,
    running=False,
    claiming=False,
    ready=False,
    role="active",
    priority=100,
    fenced=False,
) -> dict:
    return {
        "node": node,
        "role": role,
        "running": running,
        "claiming": claiming,
        "ready": ready,
        "rank": list(rank(parse(f"telegram-bot@alfred:{role}:prio={priority}"), KIND_SERVER, node)),
        "fenced": fenced,
    }


def test_active_role_outranks_standby_regardless_of_machine():
    active_on_vps = rank(parse("telegram-bot@a"), KIND_VPS, "jeeves")
    standby_on_server = rank(parse("telegram-bot@a:standby"), KIND_SERVER, "alfred")
    assert active_on_vps > standby_on_server


def test_explicit_priority_beats_the_machine_type():
    default = rank(parse("telegram-bot@a:standby"), KIND_VPS, "jeeves")
    boosted = rank(parse("telegram-bot@a:standby:prio=90"), KIND_VPS, "jeeves")
    assert boosted > default


async def test_active_takes_the_slot_immediately():
    slot = FakeSlot("telegram-bot@alfred")
    lease, events = _lease("alfred", KIND_SERVER, slot)
    await lease.tick()
    assert slot.calls == ["start"]
    assert [e[0] for e in events] == [EVENT_SINGLETON_ACTIVATED]


async def test_standby_waits_out_the_grace_period():
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
    peer = FakePeer("winpc", {SLOT: _entry("winpc", running=True)}, alive=False)
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == ["start"]


async def test_grace_restarts_when_the_active_node_blinks():
    peer_up = FakePeer("alfred", {SLOT: _entry("alfred", running=True)})
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer_up, grace_s=60.0)
    await lease.tick()
    peer_up.alive = False
    await lease.tick()
    assert slot.calls == []


async def test_running_standby_yields_to_the_returning_active_node():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, events = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == ["stop"]
    assert [e[0] for e in events] == [EVENT_SINGLETON_YIELDED]


async def test_active_claims_instead_of_starting_over_a_running_standby():
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
    await lease.tick()
    peer._singletons = {SLOT: _entry("jeeves", running=False, role="standby")}
    await lease.tick()
    assert slot.calls == ["start"]


async def test_lower_ranked_node_waits_for_the_superior_to_take_it():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", running=False)})
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    await lease.tick()
    assert slot.calls == []


def test_ready_field_reported_in_local_state():
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot)
    assert lease.local_state()[SLOT]["ready"] is False
    lease.set_ready(SLOT, True)
    assert lease.local_state()[SLOT]["ready"] is True


async def test_junior_does_not_reclaim_before_ready_timeout():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0, ready_timeout_s=9999.0)
    await lease.tick()
    assert slot.calls == ["stop"]
    await lease.tick()
    assert slot.calls == ["stop"]


async def test_junior_reclaims_after_superior_ready_timeout():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True, ready=False)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, events = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0, ready_timeout_s=0.0)
    await lease.tick()
    await lease.tick()
    assert slot.calls == ["stop", "start"]
    assert [e[0] for e in events] == [EVENT_SINGLETON_YIELDED, EVENT_SINGLETON_ACTIVATED]


async def test_junior_does_not_reclaim_if_superior_reports_ready():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True, ready=True)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0, ready_timeout_s=0.0)
    await lease.tick()
    await lease.tick()
    assert slot.calls == ["stop"]


async def test_junior_reclaim_uses_running_superior_ready_flag_too():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", running=True, ready=True)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0, ready_timeout_s=0.0)
    await lease.tick()
    await lease.tick()
    assert slot.calls == ["stop"]


async def test_yielded_since_resets_between_episodes():
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True, ready=False)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING)
    lease, _ = _lease("jeeves", KIND_VPS, slot, peer, grace_s=999.0, ready_timeout_s=0.0)
    await lease.tick()
    assert slot.calls == ["stop"]

    peer.alive = False
    peer.down_since = 0.0
    await lease.tick()
    assert slot.calls == ["stop"]

    peer.alive = True
    peer.down_since = None
    await lease.tick()
    assert slot.calls == ["stop"]


async def test_returning_superior_does_not_steal_a_self_inflicted_crash(caplog):
    peer = FakePeer("alfred", {SLOT: _entry("alfred", claiming=True)})
    slot = FakeSlot("telegram-bot@alfred:standby", status=RUNNING, process_alive=False)
    lease, events = _lease("jeeves", KIND_VPS, slot, peer, grace_s=0.0)
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.node.lease"):
        await lease.tick()
    assert slot.calls == []
    assert events == []
    assert lease.local_state()[SLOT]["claiming"] is False
    assert any("процесс уже" in r.message for r in caplog.records)


async def test_stuck_slot_warns_once_past_threshold(caplog):
    slot = FakeSlot("telegram-bot@alfred:standby")
    lease, _ = _lease("jeeves", KIND_VPS, slot, grace_s=999.0, stuck_warn_s=0.0)
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.node.lease"):
        await lease.tick()
    assert slot.calls == []
    assert any("не работает" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="sa_home_bot.node.lease"):
        await lease.tick()
    assert not any("не работает" in r.message for r in caplog.records)


async def test_stuck_tracker_resets_once_the_slot_runs_again():
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, stuck_warn_s=0.0)
    await lease.tick()
    assert slot.calls == ["start"]
    assert SLOT in lease._not_running_since

    await lease.tick()
    assert slot.calls == ["start"]
    assert SLOT not in lease._not_running_since
    assert SLOT not in lease._stuck_warned


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
    import asyncio

    import sa_home_bot.node.supervisor as sup_mod

    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(python, dash_m, module, *args, **kwargs):
        return await real_exec(python, "-c", f"import sys; sys.exit({FENCED_EXIT_CODE})", **kwargs)

    monkeypatch.setattr(sup_mod.asyncio, "create_subprocess_exec", fake_exec)

    noted: list[str] = []
    sup = Supervisor(
        ["telegram-bot@alfred"],
        None,
        emit=_noop,
        restart_delay_s=0.01,
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
    peer = FakePeer("jeeves", {SLOT: _entry("jeeves", running=True, role="standby")})
    peer.alive = False
    peer.down_since = None
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, peer)
    lease._settle_s = 0.1

    await lease.start()
    await lease.stop()
    assert slot.calls == ["start"]


async def test_settle_ends_as_soon_as_every_peer_has_answered():
    peer = FakePeer("jeeves", {SLOT: _entry("jeeves", running=True, role="standby")})
    slot = FakeSlot("telegram-bot@alfred")
    lease, _ = _lease("alfred", KIND_SERVER, slot, peer)
    lease._settle_s = 30.0

    start = time.monotonic()
    await lease.start()
    await lease.stop()
    assert time.monotonic() - start < 5.0
    assert slot.calls == []
    assert lease.local_state()[SLOT]["claiming"] is True


async def test_startup_does_not_launch_singletons_directly():
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
