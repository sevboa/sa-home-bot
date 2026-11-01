from __future__ import annotations

import asyncio

import pytest

from sa_home_bot.devlab import neighbor
from sa_home_bot.devlab.neighbor import NeighborGate
from sa_home_bot.node import events
from sa_home_bot.proto.messages import ServiceDescription, ServiceInfo, make_event
from sa_home_bot.proto.server import ProtoServer
from tests.unit.support.fakes import Clock
from tests.unit.support.waiting import wait_for, wait_until


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: set[str] = set()

    async def freeze(self) -> None:
        self.calls.append("freeze")
        if "freeze" in self.fail:
            raise RuntimeError("boom")

    async def thaw(self) -> None:
        self.calls.append("thaw")

    async def night(self, night: bool) -> None:
        self.calls.append(f"night={night}")


def build(freeze_max_s: float = 300.0) -> tuple[NeighborGate, Recorder, Clock]:
    rec = Recorder()
    clock = Clock(0.0)
    gate = NeighborGate(
        freeze=rec.freeze,
        thaw=rec.thaw,
        freeze_max_s=freeze_max_s,
        on_night=rec.night,
        clock=lambda: clock.t,
    )
    return gate, rec, clock


def task_event(name: str, task_id: str):
    return make_event(name, {events.LLM_EVENT_TASK_ID: task_id})


async def test_events_freeze_then_thaw():
    gate, rec, _ = build()
    await gate.handle_event(task_event(events.EVENT_LLM_CPU_TASK_STARTED, "a"))
    assert rec.calls == ["freeze"] and gate.frozen
    await gate.handle_event(task_event(events.EVENT_LLM_CPU_TASK_FINISHED, "a"))
    assert rec.calls == ["freeze", "thaw"] and not gate.frozen
    await gate.close()


async def test_nested_tasks_thaw_only_after_the_last():
    gate, rec, _ = build()
    for task_id in ("a", "b"):
        await gate.handle_event(task_event(events.EVENT_LLM_CPU_TASK_STARTED, task_id))
    assert rec.calls == ["freeze"]
    await gate.handle_event(task_event(events.EVENT_LLM_CPU_TASK_FINISHED, "a"))
    assert rec.calls == ["freeze"] and gate.frozen
    await gate.handle_event(task_event(events.EVENT_LLM_CPU_TASK_FINISHED, "b"))
    assert rec.calls == ["freeze", "thaw"]
    await gate.close()


async def test_finish_of_unknown_task_without_freeze_does_nothing():
    gate, rec, _ = build()
    await gate.handle_event(task_event(events.EVENT_LLM_CPU_TASK_FINISHED, "ghost"))
    assert rec.calls == []


async def test_auto_thaw_after_freeze_max_and_late_finish_is_harmless():
    gate, rec, _ = build(freeze_max_s=0.05)
    await gate.task_started("a")
    assert await wait_for(lambda: rec.calls == ["freeze", "thaw"])
    assert not gate.frozen
    await gate.task_finished("a")
    assert rec.calls == ["freeze", "thaw"]
    await gate.task_started("b")
    assert rec.calls == ["freeze", "thaw", "freeze"]
    await gate.close()


async def test_finish_cancels_auto_thaw_timer():
    gate, rec, _ = build(freeze_max_s=0.05)
    await gate.task_started("a")
    await gate.task_finished("a")
    await asyncio.sleep(0.1)
    assert rec.calls == ["freeze", "thaw"]


async def test_frozen_total_counts_finished_and_ongoing_freezes():
    gate, _, clock = build()
    await gate.task_started("a")
    clock.advance(seconds=10)
    assert gate.frozen_total() == 10
    await gate.task_finished("a")
    clock.advance(seconds=5)
    assert gate.frozen_total() == 10
    await gate.task_started("b")
    clock.advance(seconds=7)
    assert gate.frozen_total() == 17
    await gate.close()
    assert gate.frozen_total() == 17


async def test_wait_thawed_blocks_while_frozen():
    gate, _, _ = build()
    await gate.task_started("a")
    waiter = asyncio.create_task(gate.wait_thawed())
    await asyncio.sleep(0)
    assert not waiter.done()
    await gate.task_finished("a")
    await asyncio.wait_for(waiter, 1)


async def test_failed_freeze_command_does_not_break_the_gate():
    gate, rec, _ = build()
    rec.fail.add("freeze")
    await gate.task_started("a")
    await gate.task_finished("a")
    assert rec.calls == ["freeze", "thaw"]


async def test_night_events_call_back_only_on_change():
    gate, rec, _ = build()
    for night in (True, True, False):
        await gate.handle_event(
            make_event(events.EVENT_LLM_NIGHT_MODE, {events.LLM_EVENT_NIGHT: night})
        )
    assert rec.calls == ["night=True", "night=False"]


async def test_sync_replaces_tasks_and_disconnect_thaws():
    gate, rec, _ = build()
    await gate.sync(["x"], True)
    assert rec.calls == ["night=True", "freeze"]
    await gate.sync([], True)
    assert rec.calls[-1] == "thaw"
    await gate.sync(["y"], True)
    await gate.disconnected()
    assert rec.calls[-2:] == ["freeze", "thaw"] and not gate.frozen


async def test_never_frozen_clock_is_inert():
    clock = neighbor.NeverFrozen()
    assert clock.frozen is False and clock.frozen_total() == 0.0
    await clock.wait_thawed()


@pytest.fixture
async def llm_server(tmp_path):
    state = {events.LLM_STATE_CPU_TASKS: ["live"], events.LLM_STATE_NIGHT: True}

    class Service:
        def describe(self):
            return ServiceDescription(info=ServiceInfo(node="n", service="llm", version="0"))

        async def get_state(self):
            return state

        async def run_command(self, action, args):
            return {}

    server = ProtoServer(str(tmp_path / "llm.sock"), Service())
    await server.start()
    yield server, str(tmp_path / "llm.sock")
    await server.stop()


async def test_follow_llm_syncs_state_then_follows_events(llm_server):
    server, socket = llm_server
    gate, rec, _ = build()
    task = neighbor.start_following(gate, socket)
    await wait_until(lambda: rec.calls == ["night=True", "freeze"])
    await server.broadcast_event(
        events.EVENT_LLM_CPU_TASK_FINISHED, {events.LLM_EVENT_TASK_ID: "live"}
    )
    await wait_until(lambda: rec.calls[-1] == "thaw")
    await server.broadcast_event(
        events.EVENT_LLM_CPU_TASK_STARTED, {events.LLM_EVENT_TASK_ID: "n1"}
    )
    await wait_until(lambda: rec.calls[-1] == "freeze")
    await neighbor.stop_following(task, gate)
    assert not gate.frozen


async def test_follow_llm_thaws_when_connection_is_lost(llm_server):
    server, socket = llm_server
    gate, rec, _ = build()
    task = neighbor.start_following(gate, socket)
    await wait_until(lambda: gate.frozen)
    await server.stop()
    await wait_until(lambda: not gate.frozen)
    await neighbor.stop_following(task, gate)


def test_config_defaults_for_the_neighbor():
    from sa_home_bot.config import DevlabConfig, DevlabModelsConfig

    assert DevlabConfig().freeze_max_s == 300.0
    assert DevlabModelsConfig().freeze_max_s == 300.0
    assert DevlabConfig().neighbor_socket == "" == DevlabModelsConfig().neighbor_socket
