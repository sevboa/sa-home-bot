"""События монитора в боте: payload → доменная модель → нужный dispatch-вызов."""

from datetime import UTC, datetime

from sa_home_bot.bot.monitor_events import build_event_handler
from sa_home_bot.bot.monitor_state import parse_health_state, parse_outage
from sa_home_bot.domain.models import (
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    EVENT_SMART_DEGRADED,
    KIND_CPU,
    OK,
    Event,
    HealthState,
    PowerEvent,
    SmartChange,
)
from sa_home_bot.jobs.base import DispatchResult
from sa_home_bot.monitor.dispatch import event_payload, smart_change_payload
from sa_home_bot.monitor.service import _health_dict, _outage_dict
from sa_home_bot.proto.messages import make_event

NOW = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.alerts: list[Event] = []
        self.clears: list[Event] = []
        self.smarts: list[SmartChange] = []

    async def dispatch_alert(self, event):
        self.alerts.append(event)
        return DispatchResult(True, True)

    async def dispatch_clear(self, event):
        self.clears.append(event)
        return DispatchResult(True, True)

    async def dispatch_smart(self, change):
        self.smarts.append(change)
        return DispatchResult(True, True)


def _overheat(event_type: str) -> Event:
    return Event(
        type=event_type,
        component_id="cpu:pkg",
        kind=KIND_CPU,
        label="Package",
        temperature_c=95.0,
        at=NOW,
    )


async def test_overheat_roundtrip_monitor_to_bot():
    """Сериализация монитора → парсинг бота даёт исходное событие."""
    dispatcher = RecordingDispatcher()
    handle = build_event_handler(dispatcher)
    original = _overheat(EVENT_OVERHEAT_STARTED)

    await handle(make_event(EVENT_OVERHEAT_STARTED, event_payload(original)))

    assert dispatcher.alerts == [original]
    assert dispatcher.clears == []


async def test_cleared_goes_to_dispatch_clear():
    dispatcher = RecordingDispatcher()
    handle = build_event_handler(dispatcher)
    original = _overheat(EVENT_OVERHEAT_CLEARED)

    await handle(make_event(EVENT_OVERHEAT_CLEARED, event_payload(original)))

    assert dispatcher.clears == [original]


async def test_smart_change_roundtrip():
    dispatcher = RecordingDispatcher()
    handle = build_event_handler(dispatcher)
    original = SmartChange(
        component_id="disk:/dev/sdb",
        label="ST9250315AS",
        event_type=EVENT_SMART_DEGRADED,
        health_from="ok",
        health_to="warning",
        attr_changes=(),
        at=NOW,
    )

    await handle(make_event(EVENT_SMART_DEGRADED, smart_change_payload(original)))

    assert dispatcher.smarts == [original]


async def test_unknown_and_invalid_events_do_not_crash():
    dispatcher = RecordingDispatcher()
    handle = build_event_handler(dispatcher)

    await handle(make_event("service_started", {"pid": 1}))  # неизвестный тип
    await handle(make_event(EVENT_OVERHEAT_STARTED, {"garbage": True}))  # битый payload

    assert dispatcher.alerts == dispatcher.clears == dispatcher.smarts == []


def test_health_state_roundtrip_monitor_to_bot():
    state = HealthState(
        component_id="cpu:pkg",
        kind=KIND_CPU,
        label="Package",
        status=OK,
        temperature_c=41.0,
        consecutive_count=0,
        alerting_since=None,
    )
    assert parse_health_state(_health_dict(state)) == state


def test_outage_roundtrip_monitor_to_bot():
    outage = PowerEvent(
        kind="unexpected", boot_at=NOW, down_at=NOW, up_at=None, down_approx=True
    )
    assert parse_outage(_outage_dict(outage)) == outage
    assert parse_outage(None) is None
