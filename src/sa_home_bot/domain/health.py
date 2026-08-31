"""Ядро reconciliation: срез датчиков + известное состояние из БД → diff.

Здесь же реализован анти-дребезг (гистерезис): переход фиксируется только если
показание держится в нужной зоне N подряд снятых срезов. Чистые функции, без БД,
сети и aiogram — тестируются изолированно.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sa_home_bot.domain.hysteresis import reconcile_band
from sa_home_bot.domain.models import (
    ALERTING,
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    OK,
    Event,
    HealthDiff,
    HealthState,
    KnownState,
    SensorReading,
    Transition,
)
from sa_home_bot.domain.policy import ComponentPolicy

PolicyResolver = Callable[[SensorReading], ComponentPolicy]


def _reconcile_one(
    reading: SensorReading,
    known: KnownState | None,
    cpolicy: ComponentPolicy,
    now: datetime,
) -> tuple[HealthState, Transition | None]:
    band = cpolicy.policy.band(reading)
    prev_status = known.status if known else OK
    temp = reading.temperature_c

    res = reconcile_band(
        band,
        prev_status=prev_status,
        prev_count=known.consecutive_count if known else 0,
        alerting_since=known.alerting_since if known else None,
        consecutive_to_alert=cpolicy.consecutive_to_alert,
        consecutive_to_clear=cpolicy.consecutive_to_clear,
        now=now,
    )

    new_state = HealthState(
        component_id=reading.component_id,
        kind=reading.kind,
        label=reading.label,
        status=res.status,
        temperature_c=temp,
        consecutive_count=res.consecutive_count,
        alerting_since=res.alerting_since,
    )
    transition: Transition | None = None
    if res.transitioned_to is not None:
        transition = Transition(
            component_id=reading.component_id,
            kind=reading.kind,
            label=reading.label,
            from_status=prev_status,
            to_status=res.transitioned_to,
            temperature_c=temp,
            at=now,
        )
    return new_state, transition


def compute_health_diff(
    current: list[SensorReading],
    known: dict[str, KnownState],
    resolve_policy: PolicyResolver,
    now: datetime,
) -> HealthDiff:
    """Сравнить срез показаний с известным состоянием, вернуть новый срез + переходы."""
    states: list[HealthState] = []
    transitions: list[Transition] = []
    for reading in current:
        cpolicy = resolve_policy(reading)
        new_state, transition = _reconcile_one(
            reading, known.get(reading.component_id), cpolicy, now
        )
        states.append(new_state)
        if transition is not None:
            transitions.append(transition)
    return HealthDiff(states=states, transitions=transitions)


def classify_events(transitions: list[Transition]) -> list[Event]:
    """Превратить переходы в события для рассылки."""
    events: list[Event] = []
    for tr in transitions:
        if tr.from_status == OK and tr.to_status == ALERTING:
            event_type = EVENT_OVERHEAT_STARTED
        elif tr.from_status == ALERTING and tr.to_status == OK:
            event_type = EVENT_OVERHEAT_CLEARED
        else:
            continue
        events.append(
            Event(
                type=event_type,
                component_id=tr.component_id,
                kind=tr.kind,
                label=tr.label,
                temperature_c=tr.temperature_c,
                at=tr.at,
            )
        )
    return events
