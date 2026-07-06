"""ProtoEventDispatcher: события уходят broadcast'ом, без клиентов — pending."""

from datetime import UTC, datetime

from sa_home_bot.domain.models import (
    DISK_OK,
    DISK_WARN,
    EVENT_OVERHEAT_STARTED,
    EVENT_SMART_DEGRADED,
    KIND_CPU,
    Event,
    SmartAttrChange,
    SmartChange,
)
from sa_home_bot.monitor.dispatch import ProtoEventDispatcher

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


class FakeServer:
    def __init__(self, clients: int) -> None:
        self._clients = clients
        self.events: list[tuple[str, dict]] = []

    async def broadcast_event(self, event_type, data=None):
        self.events.append((event_type, data or {}))
        return self._clients


def _event() -> Event:
    return Event(
        type=EVENT_OVERHEAT_STARTED,
        component_id="cpu:pkg",
        kind=KIND_CPU,
        label="Package",
        temperature_c=95.0,
        at=NOW,
    )


def _smart_change() -> SmartChange:
    return SmartChange(
        component_id="disk:/dev/sdb",
        label="ST9250315AS",
        event_type=EVENT_SMART_DEGRADED,
        health_from=DISK_OK,
        health_to=DISK_WARN,
        attr_changes=(SmartAttrChange(attr_id=197, name="Current_Pending_Sector", old=0, new=3),),
        at=NOW,
    )


async def test_alert_broadcast_when_client_connected():
    server = FakeServer(clients=1)
    result = await ProtoEventDispatcher(server).dispatch_alert(_event())
    assert result.delivered and result.handled
    event_type, data = server.events[0]
    assert event_type == EVENT_OVERHEAT_STARTED
    assert data["component_id"] == "cpu:pkg"
    assert data["temperature_c"] == 95.0
    assert data["at"] == NOW.isoformat()


async def test_no_clients_keeps_event_pending():
    server = FakeServer(clients=0)
    result = await ProtoEventDispatcher(server).dispatch_alert(_event())
    # handled=False → job не пометит notified, событие повторится.
    assert not result.delivered and not result.handled


async def test_smart_change_payload():
    server = FakeServer(clients=2)
    result = await ProtoEventDispatcher(server).dispatch_smart(_smart_change())
    assert result.delivered and result.handled
    event_type, data = server.events[0]
    assert event_type == EVENT_SMART_DEGRADED
    assert data["health_to"] == DISK_WARN
    assert data["attr_changes"][0]["attr_id"] == 197
