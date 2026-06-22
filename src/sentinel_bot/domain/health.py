"""Ядро reconciliation: срез датчиков + известное состояние из БД → diff.

Здесь же реализован анти-дребезг (гистерезис): переход фиксируется только если
показание держится в нужной зоне N подряд снятых срезов. Чистые функции, без БД,
сети и aiogram — тестируются изолированно.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sentinel_bot.domain.models import (
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
from sentinel_bot.domain.policy import BAND_OVER, BAND_UNDER, ComponentPolicy

PolicyResolver = Callable[[SensorReading], ComponentPolicy]


def _reconcile_one(
    reading: SensorReading,
    known: KnownState | None,
    cpolicy: ComponentPolicy,
    now: datetime,
) -> tuple[HealthState, Transition | None]:
    band = cpolicy.policy.band(reading)
    prev_status = known.status if known else OK
    prev_count = known.consecutive_count if known else 0
    alerting_since = known.alerting_since if known else None
    temp = reading.temperature_c

    def state(status: str, count: int, since: datetime | None) -> HealthState:
        return HealthState(
            component_id=reading.component_id,
            kind=reading.kind,
            label=reading.label,
            status=status,
            temperature_c=temp,
            consecutive_count=count,
            alerting_since=since,
        )

    if prev_status == OK:
        # Счётчик копит подряд идущие OVER-срезы.
        if band == BAND_OVER:
            count = prev_count + 1
            if count >= cpolicy.consecutive_to_alert:
                transition = Transition(
                    component_id=reading.component_id,
                    kind=reading.kind,
                    label=reading.label,
                    from_status=OK,
                    to_status=ALERTING,
                    temperature_c=temp,
                    at=now,
                )
                return state(ALERTING, 0, now), transition
            return state(OK, count, None), None
        # MID или UNDER — серия прервалась.
        return state(OK, 0, None), None

    # prev_status == ALERTING: счётчик копит подряд идущие UNDER-срезы.
    if band == BAND_UNDER:
        count = prev_count + 1
        if count >= cpolicy.consecutive_to_clear:
            transition = Transition(
                component_id=reading.component_id,
                kind=reading.kind,
                label=reading.label,
                from_status=ALERTING,
                to_status=OK,
                temperature_c=temp,
                at=now,
            )
            return state(OK, 0, None), transition
        return state(ALERTING, count, alerting_since), None
    # OVER или MID — остаёмся в alerting, серия остывания прервалась.
    return state(ALERTING, 0, alerting_since), None


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
