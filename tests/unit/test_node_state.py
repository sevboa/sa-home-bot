"""node/state.py — персистентное состояние ноды (assignments, пиры)."""

from sa_home_bot.config import SwarmNodeConfig
from sa_home_bot.node.state import NodeState


def test_load_missing_file_returns_empty_state(tmp_path):
    state = NodeState.load(tmp_path / "node-state.json")
    assert state.assignments == []
    assert state.peers == []


def test_save_then_load_roundtrip(tmp_path):
    path = tmp_path / "sub" / "node-state.json"  # каталог ещё не существует
    state = NodeState(
        assignments=["apps", "monitor"],
        peers=[SwarmNodeConfig(id="arch-t480", endpoint="tcp://100.110.58.31:8710")],
    )
    state.save(path)

    loaded = NodeState.load(path)
    assert loaded.assignments == ["apps", "monitor"]
    assert loaded.peers == [SwarmNodeConfig(id="arch-t480", endpoint="tcp://100.110.58.31:8710")]


def test_save_is_atomic_no_leftover_tmp_files(tmp_path):
    path = tmp_path / "node-state.json"
    NodeState(assignments=["apps"]).save(path)
    leftovers = list(tmp_path.glob(".*.tmp"))
    assert leftovers == []
    assert path.exists()


def test_save_overwrites_previous_content(tmp_path):
    path = tmp_path / "node-state.json"
    NodeState(assignments=["apps"]).save(path)
    NodeState(assignments=["monitor"]).save(path)
    assert NodeState.load(path).assignments == ["monitor"]
