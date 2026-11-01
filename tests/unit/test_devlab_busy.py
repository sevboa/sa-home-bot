from __future__ import annotations

import asyncio
import shutil
import tempfile

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.devlab.executor.service import ExecutorService
from sa_home_bot.node import peers as peers_mod
from sa_home_bot.node import service as service_module
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.service import NodeService
from sa_home_bot.proto.messages import ServiceInfo
from sa_home_bot.proto.server import ProtoServer
from tests.unit.support.waiting import wait_for
from tests.unit.test_node_service import _fake_supervisor

OWNER_WAIT_S = 100.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tracker(clock):
    return BusyTracker(OWNER_WAIT_S, clock)


def test_idle_by_default(tracker):
    assert not tracker.busy()


def test_running_job_is_busy_until_finished(tracker):
    tracker.started("a")
    assert tracker.busy()
    tracker.finished("a")
    assert not tracker.busy()


def test_waiting_owner_holds_within_owner_wait_and_not_after(tracker, clock):
    tracker.started("a")
    tracker.waiting_owner("a")
    assert tracker.busy()
    clock.now += OWNER_WAIT_S - 1
    assert tracker.busy()
    clock.now += 1
    assert not tracker.busy()


def test_release_stops_holding(tracker):
    tracker.waiting_owner("a")
    tracker.released("a")
    assert not tracker.busy()


def test_one_released_job_does_not_free_another(tracker):
    tracker.waiting_owner("a")
    tracker.waiting_owner("b")
    tracker.released("a")
    assert tracker.holding_for_owner() == ["b"]


def test_queue_holds(tracker):
    tracker.set_queued(2)
    assert tracker.busy()
    tracker.set_queued(0)
    assert not tracker.busy()


def test_outbox_holds_even_after_release(tracker):
    pending = [1]
    tracker.outbox_size = lambda: len(pending)
    tracker.waiting_owner("a")
    tracker.released("a")
    assert tracker.busy()
    pending.clear()
    assert not tracker.busy()


def test_restore_keeps_original_question_time(tracker, clock):
    tracker.restore({"a": clock.now - OWNER_WAIT_S - 1, "b": clock.now - 1})
    assert tracker.holding_for_owner() == ["b"]


async def test_service_reports_busy_in_hello_and_state(tracker):
    service = ExecutorService("mycraft", tracker)
    assert not service.describe().info.busy
    tracker.started("a")
    payload = service.describe().info.to_payload()
    assert payload[protocol.FIELD_BUSY] is True
    state = await service.get_state()
    assert state[protocol.FIELD_BUSY] is True
    assert state[protocol.FIELD_JOBS] == ["a"]


def test_old_hello_without_busy_means_not_busy():
    payload = ServiceInfo(node="n", service="s", version="1").to_payload()
    assert protocol.FIELD_BUSY not in payload
    assert not ServiceInfo.from_payload(payload).busy
    assert ServiceInfo.from_payload({**payload, protocol.FIELD_BUSY: True}).busy


class StubLink:
    def __init__(self, alive: bool, busy: bool) -> None:
        self.alive = alive
        self.busy = busy


def test_router_lists_only_alive_busy_local_services():
    router = NodeRouter(
        "n",
        local_services={
            "devlab": StubLink(True, True),
            "llm": StubLink(True, False),
            "gone": StubLink(False, True),
        },
    )
    assert router.busy_services() == ["devlab"]


@pytest.mark.parametrize(
    ("links", "powered_off"),
    [
        ({"devlab": StubLink(True, True)}, False),
        ({"devlab": StubLink(True, False)}, True),
        ({}, True),
    ],
)
async def test_auto_poweroff_respects_busy_services(monkeypatch, links, powered_off):
    async def no_sessions():
        return []

    monkeypatch.setattr(service_module, "POWER_DELAY_S", 0.0)
    monkeypatch.setattr(service_module.ssh_sessions, "list_ssh_sessions", no_sessions)
    monkeypatch.setattr(service_module.ssh_sessions, "list_tmux_sessions", no_sessions)
    ran: list[list[str]] = []
    emitted: list[str] = []

    async def power_runner(argv):
        ran.append(argv)

    async def emit(event_type, data):
        emitted.append(event_type)

    sup, _ = _fake_supervisor()
    svc = NodeService(
        sup,
        NodeRouter("n", local_services=links),
        power_runner=power_runner,
        node_kind="workstation",
        idle_poweroff=True,
        emit=emit,
    )
    await svc.maybe_auto_poweroff_idle()
    await asyncio.sleep(0.05)
    assert bool(ran) is powered_off
    assert emitted == []


async def test_peer_link_follows_executor_busy(tracker, monkeypatch):
    monkeypatch.setattr(peers_mod, "HEARTBEAT_INTERVAL_S", 0.02)
    sock_dir = tempfile.mkdtemp()
    server = ProtoServer(f"unix://{sock_dir}/devlab.sock", ExecutorService("mycraft", tracker))
    await server.start()
    link = PeerLink(protocol.EXECUTOR_SERVICE, f"unix://{sock_dir}/devlab.sock")
    await link.start()
    try:
        assert await wait_for(lambda: link.alive)
        assert not link.busy
        tracker.started("a")
        assert await wait_for(lambda: link.busy)
        tracker.finished("a")
        assert await wait_for(lambda: not link.busy)
    finally:
        await link.stop()
        await server.stop()
        shutil.rmtree(sock_dir, ignore_errors=True)
