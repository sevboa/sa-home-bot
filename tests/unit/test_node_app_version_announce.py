from __future__ import annotations

from sa_home_bot.node import app as node_app
from sa_home_bot.node.service import EVENT_RESTART_APPLIED
from sa_home_bot.node.state import NodeState


class _FakeBroadcast:
    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail_times = fail_times

    async def __call__(self, event_type: str, data: dict) -> int:
        self.calls.append((event_type, data))
        return 0 if len(self.calls) <= self._fail_times else 1


async def test_silent_on_very_first_start_with_no_recorded_version(tmp_path):
    path = tmp_path / "node-state.json"
    state = NodeState()
    broadcast = _FakeBroadcast()

    await node_app._announce_version_if_changed(state, path, broadcast)

    assert broadcast.calls == []
    assert NodeState.load(path).last_known_version == node_app.__version__


async def test_silent_when_version_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(node_app, "__version__", "0.72.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    broadcast = _FakeBroadcast()

    await node_app._announce_version_if_changed(state, path, broadcast)

    assert broadcast.calls == []


async def test_emits_restart_applied_when_version_changed_and_delivered_first_try(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(node_app, "__version__", "0.73.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    broadcast = _FakeBroadcast()

    await node_app._announce_version_if_changed(state, path, broadcast)

    assert broadcast.calls == [(EVENT_RESTART_APPLIED, {"from": "0.72.0", "to": "0.73.0"})]
    assert NodeState.load(path).last_known_version == "0.73.0"
    broadcast.calls.clear()
    await node_app._announce_version_if_changed(NodeState.load(path), path, broadcast)
    assert broadcast.calls == []


async def test_retries_until_a_recipient_is_actually_connected(tmp_path, monkeypatch):
    monkeypatch.setattr(node_app, "__version__", "0.73.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    broadcast = _FakeBroadcast(fail_times=3)

    await node_app._announce_version_if_changed(state, path, broadcast, retry_s=0.01, window_s=10.0)

    assert len(broadcast.calls) == 4


async def test_state_persisted_immediately_even_if_delivery_never_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(node_app, "__version__", "0.73.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    broadcast = _FakeBroadcast(fail_times=999)

    await node_app._announce_version_if_changed(state, path, broadcast, retry_s=0.01, window_s=0.03)

    assert len(broadcast.calls) >= 2
    assert NodeState.load(path).last_known_version == "0.73.0"
