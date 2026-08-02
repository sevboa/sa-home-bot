"""Тип машины ноды: свойства, фильтр назначений, алерт о пропаже."""

from __future__ import annotations

import pytest

from sa_home_bot.node.kind import KIND_SERVER, KIND_VPS, KIND_WORKSTATION, traits_for
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.service import ACTION_ASSIGN, NodeService
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.node.watch import EVENT_NODE_DOWN, EVENT_NODE_UP, PresenceWatcher


class FakeLink:
    """Пир с управляемой живостью и временем недоступности."""

    def __init__(self, name: str, *, alive: bool, kind: str = "", down_s: float | None = None):
        self.name = name
        self.endpoint = f"tcp://{name}:8710"
        self.endpoints = [self.endpoint]
        self.alive = alive
        self.node_kind = kind
        self.left = False
        self._down_s = down_s

    def downtime_s(self) -> float | None:
        return None if self.alive else self._down_s


def _router(node_id: str, *links: FakeLink) -> NodeRouter:
    return NodeRouter(node_id, peers={link.name: link for link in links})


def _watcher(node_id: str, *links: FakeLink, **kw) -> tuple[PresenceWatcher, list[tuple]]:
    events: list[tuple] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    kw.setdefault("down_after_s", 300.0)
    return PresenceWatcher(node_id, _router(node_id, *links), emit=emit, **kw), events


# --- traits -----------------------------------------------------------------


def test_server_and_vps_are_expected_online_workstation_is_not():
    assert traits_for(KIND_SERVER).alerts_when_unreachable
    assert traits_for(KIND_VPS).alerts_when_unreachable
    assert not traits_for(KIND_WORKSTATION).alerts_when_unreachable


def test_only_workstation_is_wakeable():
    assert traits_for(KIND_WORKSTATION).wakeable
    assert not traits_for(KIND_SERVER).wakeable
    assert not traits_for(KIND_VPS).wakeable


def test_vps_has_no_hardware_sensors():
    assert not traits_for(KIND_VPS).hardware_sensors
    assert traits_for(KIND_SERVER).hardware_sensors


def test_lease_priority_prefers_the_machine_more_likely_to_be_on():
    assert (
        traits_for(KIND_SERVER).lease_priority
        > traits_for(KIND_VPS).lease_priority
        > traits_for(KIND_WORKSTATION).lease_priority
    )


def test_unknown_kind_is_conservative():
    """Нода старой версии не шумит алертами и не отбирает синглтон."""
    unknown = traits_for("")
    assert not unknown.alerts_when_unreachable
    assert not unknown.wakeable
    assert unknown.lease_priority == 0
    assert traits_for("нечто") == unknown


# --- фильтр назначений по типу ----------------------------------------------


def _assign_choices(kind: str) -> tuple[str, ...]:
    svc = NodeService(Supervisor([], None, emit=_noop), node_kind=kind)
    action = next(a for a in svc.describe().actions if a.id == ACTION_ASSIGN)
    return action.params[0].choices


def test_monitor_is_not_offered_on_a_machine_without_sensors():
    assert "monitor" in _assign_choices(KIND_SERVER)
    assert "monitor" not in _assign_choices(KIND_VPS)
    # Остальные службы на VDS предлагаются как обычно.
    assert "telegram-bot" in _assign_choices(KIND_VPS)


def test_node_reports_its_kind_in_hello_and_state():
    svc = NodeService(Supervisor([], None, emit=_noop), node_kind=KIND_VPS)
    assert svc.describe().info.node_kind == KIND_VPS


async def test_get_state_carries_kind():
    svc = NodeService(Supervisor([], None, emit=_noop), node_kind=KIND_VPS)
    assert (await svc.get_state())["kind"] == KIND_VPS


# --- наблюдатель присутствия ------------------------------------------------


async def test_sleeping_workstation_raises_no_alarm():
    watcher, events = _watcher(
        "alfred", FakeLink("winpc", alive=False, kind=KIND_WORKSTATION, down_s=9999)
    )
    await watcher.check_once()
    assert events == []


async def test_missing_always_on_node_raises_node_down():
    watcher, events = _watcher("alfred", FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600))
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]
    assert events[0][1]["node"] == "jeeves"


async def test_node_down_waits_for_the_threshold():
    watcher, events = _watcher(
        "alfred", FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=120), down_after_s=300.0
    )
    await watcher.check_once()
    assert events == []


async def test_node_down_is_reported_once_then_node_up_on_return():
    link = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    watcher, events = _watcher("alfred", link)
    await watcher.check_once()
    await watcher.check_once()  # ещё лежит — повторно не сообщаем
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]
    link.alive = True
    await watcher.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN, EVENT_NODE_UP]


async def test_only_the_lowest_id_alive_node_announces():
    """Иначе о каждой пропаже пришло бы по сообщению от каждого соседа:
    SeenEvents дедуплицирует одно событие, но не три разных об одном факте."""
    down = FakeLink("jeeves", alive=False, kind=KIND_VPS, down_s=600)
    peer = FakeLink("winpc", alive=True, kind=KIND_WORKSTATION)

    quiet, quiet_events = _watcher("zeta", down, peer)  # "winpc" < "zeta" — молчим
    await quiet.check_once()
    assert quiet_events == []

    loud, loud_events = _watcher("alfred", down, peer)  # "alfred" — наименьший
    await loud.check_once()
    assert [e[0] for e in loud_events] == [EVENT_NODE_DOWN]


async def test_kind_of_an_unreachable_peer_is_remembered(tmp_path):
    """Тип нужен именно тогда, когда ноду уже не спросить, — значит он обязан
    пережить рестарт."""
    state_path = tmp_path / "node-state.json"
    state = NodeState()
    link = FakeLink("jeeves", alive=True, kind=KIND_VPS)
    watcher, _ = _watcher("alfred", link, state=state, state_path=str(state_path))
    await watcher.check_once()

    reloaded = NodeState.load(state_path)
    assert [(p.id, p.kind) for p in reloaded.peers] == [("jeeves", KIND_VPS)]

    # Связь потеряна, тип забыт линком — но известен из состояния, поэтому
    # алерт всё равно поднимается.
    link.alive = False
    link.node_kind = ""
    link._down_s = 600
    watcher2, events = _watcher(
        "alfred", link, state=reloaded, state_path=str(state_path)
    )
    await watcher2.check_once()
    assert [e[0] for e in events] == [EVENT_NODE_DOWN]


@pytest.mark.parametrize("kind", [KIND_SERVER, KIND_WORKSTATION, KIND_VPS])
def test_config_accepts_every_known_kind(kind):
    from sa_home_bot.config import NodeConfig

    assert NodeConfig(kind=kind).kind == kind


def test_config_rejects_an_unknown_kind():
    from pydantic import ValidationError

    from sa_home_bot.config import NodeConfig

    with pytest.raises(ValidationError):
        NodeConfig(kind="toaster")


def test_config_power_controllable_defaults_to_none():
    from sa_home_bot.config import NodeConfig

    assert NodeConfig().power_controllable is None
    assert NodeConfig(power_controllable=True).power_controllable is True
    assert NodeConfig(power_controllable=False).power_controllable is False


async def _noop(event_type: str, data: dict) -> None:
    return None
