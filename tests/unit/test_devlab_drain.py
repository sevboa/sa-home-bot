from __future__ import annotations

import pytest

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.devlab.executor.service import ExecutorService
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, ProtoError

EXIT_TIMEOUT = 124


def make_service(tracker: BusyTracker):
    from sa_home_bot.devlab.executor.park import ParkMode

    park = ParkMode(active=False)
    return ExecutorService("mycraft", tracker, park=park), park


async def test_park_action_turns_on_park_mode():
    tracker = BusyTracker()
    tracker.set_queued(2)
    service, park = make_service(tracker)
    assert (await service.get_state())[protocol.FIELD_BUSY]

    answer = await service.run_command(protocol.ACTION_PARK, {})

    assert answer == {protocol.FIELD_PARKED: True}
    assert park.active
    assert not (await service.get_state())[protocol.FIELD_BUSY]


async def test_park_action_twice_answers_the_same():
    service, park = make_service(BusyTracker())
    await service.run_command(protocol.ACTION_PARK, {})
    assert await service.run_command(protocol.ACTION_PARK, {}) == {protocol.FIELD_PARKED: True}
    assert park.active


async def test_unknown_action_still_rejected():
    service, park = make_service(BusyTracker())
    with pytest.raises(ProtoError) as caught:
        await service.run_command("nope", {})
    assert caught.value.code == ERR_UNKNOWN_ACTION
    assert not park.active


class Node:
    def __init__(self, states, park_error=None, state_error=None):
        self.states = list(states)
        self.park_error = park_error
        self.state_error = state_error
        self.calls: list[str] = []
        self.slept: list[float] = []
        self.now = 0.0

    def park(self):
        self.calls.append("park")
        if self.park_error:
            raise self.park_error
        return {protocol.FIELD_PARKED: True}

    def state(self):
        self.calls.append("state")
        if self.state_error:
            raise self.state_error
        busy = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {protocol.FIELD_BUSY: busy}

    def restart(self):
        self.calls.append("restart")

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now


def drain(node, **kwargs):
    from sa_home_bot.devlab.drain import drain_command

    return drain_command(
        park=node.park,
        state=node.state,
        restart=node.restart,
        sleep=node.sleep,
        clock=node.clock,
        **kwargs,
    )


def test_drain_parks_waits_until_free_then_restarts():
    node = Node([True, True, False])
    assert drain(node, interval=5.0, timeout=60.0) == 0
    assert node.calls == ["park", "state", "state", "state", "restart"]
    assert node.slept == [5.0, 5.0]


def test_drain_timeout_exits_124_without_restart(capsys):
    node = Node([True])
    assert drain(node, interval=5.0, timeout=12.0) == EXIT_TIMEOUT
    assert "restart" not in node.calls
    assert node.calls[0] == "park"
    assert capsys.readouterr().err


@pytest.mark.parametrize(
    ("park_error", "state_error"),
    [(OSError("нет сокета"), None), (None, ProtoError("unavailable", "служба не отвечает"))],
)
def test_drain_connection_error_exits_1_without_restart(park_error, state_error, capsys):
    node = Node([True], park_error=park_error, state_error=state_error)
    assert drain(node, interval=5.0, timeout=60.0) == 1
    assert "restart" not in node.calls
    assert capsys.readouterr().err


def test_executor_service_declares_park_for_proto_server():
    service = ExecutorService("mycraft", BusyTracker())
    assert service.describe().find_action(protocol.ACTION_PARK) is not None
