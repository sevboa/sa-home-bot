"""node/app.py::_announce_version_if_changed — событие restart_applied.

Решение пользователя 2026-08-04: Альфред не должен держать открытый ответ,
ожидая рестарт ноды синхронно — вместо этого нода сама объявляет о смене
версии один раз при старте, а remind(after_event=...) на это просыпается
(bot/tools.py, bot/node_events.py).

Живой баг 2026-08-05: событие ни разу не долетело до бота — рассылка
уходила сразу после server.start(), когда соседи/локальные службы ещё не
успели переподключиться после рестарта (ProtoServer.broadcast_event шлёт
только уже аутентифицированным соединениям, их в этот момент обычно 0).
Функция теперь повторяет рассылку до появления хотя бы одного получателя
или до истечения окна — это здесь и проверяется, без сети/событий роя
(``broadcast`` — двойник, возвращающий число доставок, как настоящий
ProtoServer.broadcast_event).
"""

from __future__ import annotations

from sa_home_bot.node import app as node_app
from sa_home_bot.node.service import EVENT_RESTART_APPLIED
from sa_home_bot.node.state import NodeState


class _FakeBroadcast:
    """Двойник ProtoServer.broadcast_event: первые ``fail_times`` вызовов
    возвращают 0 доставок (никто не подключён), дальше — 1."""

    def __init__(self, fail_times: int = 0) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._fail_times = fail_times

    async def __call__(self, event_type: str, data: dict) -> int:
        self.calls.append((event_type, data))
        return 0 if len(self.calls) <= self._fail_times else 1


async def test_silent_on_very_first_start_with_no_recorded_version(tmp_path):
    path = tmp_path / "node-state.json"
    state = NodeState()  # last_known_version ещё None
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
    # Новая версия персистится — второй подряд вызов уже молчит.
    assert NodeState.load(path).last_known_version == "0.73.0"
    broadcast.calls.clear()
    await node_app._announce_version_if_changed(NodeState.load(path), path, broadcast)
    assert broadcast.calls == []


async def test_retries_until_a_recipient_is_actually_connected(tmp_path, monkeypatch):
    # Живой баг 2026-08-05: первые попытки (соседи ещё не переподключились)
    # не должны считаться успехом — функция обязана повторять, пока
    # доставка не пойдёт хоть кому-то.
    monkeypatch.setattr(node_app, "__version__", "0.73.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    broadcast = _FakeBroadcast(fail_times=3)

    await node_app._announce_version_if_changed(state, path, broadcast, retry_s=0.01, window_s=10.0)

    assert len(broadcast.calls) == 4  # 3 неудачных + успешная


async def test_state_persisted_immediately_even_if_delivery_never_succeeds(tmp_path, monkeypatch):
    # Версия РЕАЛЬНО сменилась независимо от того, долетело ли уведомление —
    # повторный рестарт до истечения окна не должен посчитать это "новой"
    # сменой версии (проверяется отдельно тестом на молчание).
    monkeypatch.setattr(node_app, "__version__", "0.73.0")
    path = tmp_path / "node-state.json"
    state = NodeState(last_known_version="0.72.0")
    broadcast = _FakeBroadcast(fail_times=999)  # никогда не доставляется

    await node_app._announce_version_if_changed(state, path, broadcast, retry_s=0.01, window_s=0.03)

    assert len(broadcast.calls) >= 2  # повторяли, не сдались с первой попытки
    assert NodeState.load(path).last_known_version == "0.73.0"
