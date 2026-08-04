"""node/app.py::_announce_version_if_changed — событие restart_applied.

Решение пользователя 2026-08-04: Альфред не должен держать открытый ответ,
ожидая рестарт ноды синхронно — вместо этого нода сама объявляет о смене
версии один раз при старте, а remind(after_event=...) на это просыпается
(bot/tools.py, bot/node_events.py). Здесь — только сам факт эмита/молчания
и персистентность last_known_version, без сети/событий роя.
"""

from __future__ import annotations

from sa_home_bot.node import app as node_app
from sa_home_bot.node.service import EVENT_RESTART_APPLIED
from sa_home_bot.node.state import NodeState


class _Emitted:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, event_type: str, data: dict) -> None:
        self.calls.append((event_type, data))


async def test_silent_on_very_first_start_with_no_recorded_version(tmp_path):
    path = tmp_path / "node-state.json"
    state = NodeState()  # last_known_version ещё None
    emit = _Emitted()

    await node_app._announce_version_if_changed(state, path, emit)

    assert emit.calls == []
    assert NodeState.load(path).last_known_version == node_app.__version__


async def test_silent_when_version_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(node_app, "__version__", "0.72.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    emit = _Emitted()

    await node_app._announce_version_if_changed(state, path, emit)

    assert emit.calls == []


async def test_emits_restart_applied_when_version_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(node_app, "__version__", "0.73.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    emit = _Emitted()

    await node_app._announce_version_if_changed(state, path, emit)

    assert emit.calls == [(EVENT_RESTART_APPLIED, {"from": "0.72.0", "to": "0.73.0"})]
    # Новая версия персистится — второй подряд вызов уже молчит.
    assert NodeState.load(path).last_known_version == "0.73.0"
    emit.calls.clear()
    await node_app._announce_version_if_changed(NodeState.load(path), path, emit)
    assert emit.calls == []
