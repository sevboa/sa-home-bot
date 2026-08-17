"""node/service.py::_trigger_peers — generic fan-out команды службе на себе
и на живых пирах (доставка/skip/unreachable), см. ACTION_TRIGGER_PEERS."""

from __future__ import annotations

import pytest

from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.service import NodeService
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import (
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Envelope,
    ProtoError,
    make_error_response,
    make_response,
)


class _FakePeer:
    """Двойник PeerLink: только то, что реально читает NodeRouter/
    _trigger_peers (peers_state + forward), без настоящего сокета."""

    def __init__(
        self,
        name: str,
        *,
        alive: bool = True,
        ok: bool = True,
        payload: dict | None = None,
        raise_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.endpoint = f"unix:///tmp/{name}.sock"
        self.endpoints = (self.endpoint,)
        self.alive = alive
        self.node_kind = "workstation"
        self.left = False
        self.wake_info = None
        self._ok = ok
        self._payload = payload or {}
        self._raise = raise_error
        self.calls: list[Envelope] = []

    def downtime_s(self) -> float:
        return 0.0

    async def forward(self, env: Envelope) -> Envelope:
        self.calls.append(env)
        if self._raise is not None:
            raise self._raise
        if self._ok:
            return make_response(env, self._payload)
        return make_error_response(env.id, ERR_UNKNOWN_DST, "нет такой службы")


def _fake_supervisor() -> Supervisor:
    async def emit(event_type, data):
        pass

    return Supervisor(["monitor"], "config.toml", emit=emit)


async def test_dispatches_to_self_when_local_service_present():
    router = NodeRouter("jeeves", local_services={"vpn_check": _FakePeer("vpn_check")})
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    result = await svc.run_command(
        "trigger_peers", {"service": "vpn_check", "action": "check", "args": {}}
    )
    assert result == {"dispatched": ["jeeves"], "skipped": [], "unreachable": []}


async def test_skips_self_when_no_local_service():
    router = NodeRouter("jeeves")  # local_services пуст
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    result = await svc.run_command(
        "trigger_peers", {"service": "vpn_check", "action": "check", "args": {}}
    )
    assert result == {"dispatched": [], "skipped": ["jeeves"], "unreachable": []}


async def test_dispatches_to_reachable_peer_with_service():
    peer = _FakePeer("alfred", ok=True, payload={"accepted": True})
    router = NodeRouter("jeeves", peers={"alfred": peer})
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    result = await svc.run_command(
        "trigger_peers", {"service": "vpn_check", "action": "check", "args": {"targets": ["x"]}}
    )
    assert result == {"dispatched": ["alfred"], "skipped": ["jeeves"], "unreachable": []}
    assert peer.calls[0].payload == {"action": "check", "args": {"targets": ["x"]}}


async def test_skips_peer_without_service():
    peer = _FakePeer(
        "mycraft", raise_error=ProtoError(ERR_UNKNOWN_DST, "нет такой службы: vpn_check")
    )
    router = NodeRouter("jeeves", peers={"mycraft": peer})
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    result = await svc.run_command(
        "trigger_peers", {"service": "vpn_check", "action": "check", "args": {}}
    )
    assert result["skipped"] == ["jeeves", "mycraft"]
    assert result["dispatched"] == []
    assert result["unreachable"] == []


async def test_marks_unreachable_peer_without_failing_whole_call():
    ok_peer = _FakePeer("alfred", ok=True)
    down_peer = _FakePeer("jeeves2", raise_error=ProtoError(ERR_UNAVAILABLE, "нет соединения"))
    router = NodeRouter("jeeves", peers={"alfred": ok_peer, "jeeves2": down_peer})
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    result = await svc.run_command(
        "trigger_peers", {"service": "vpn_check", "action": "check", "args": {}}
    )
    assert result["dispatched"] == ["alfred"]
    assert result["unreachable"] == ["jeeves2"]


async def test_dead_peer_is_not_even_attempted():
    dead = _FakePeer("winpc", alive=False)
    router = NodeRouter("jeeves", peers={"winpc": dead})
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    result = await svc.run_command(
        "trigger_peers", {"service": "vpn_check", "action": "check", "args": {}}
    )
    assert "winpc" not in result["dispatched"]
    assert "winpc" not in result["skipped"]
    assert "winpc" not in result["unreachable"]
    assert dead.calls == []


async def test_missing_service_arg_is_bad_request():
    router = NodeRouter("jeeves")
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    with pytest.raises(ProtoError):
        await svc.run_command("trigger_peers", {"action": "check"})


async def test_missing_action_arg_is_bad_request():
    router = NodeRouter("jeeves")
    svc = NodeService(_fake_supervisor(), router, node_id="jeeves")
    with pytest.raises(ProtoError):
        await svc.run_command("trigger_peers", {"service": "vpn_check"})


def test_describe_declares_trigger_peers_without_choices():
    router = NodeRouter("jeeves")
    action = NodeService(_fake_supervisor(), router, node_id="jeeves").describe().find_action(
        "trigger_peers"
    )
    assert action is not None
    assert [p.name for p in action.params] == ["service", "action"]
