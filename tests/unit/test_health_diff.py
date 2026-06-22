from datetime import timedelta

from sentinel_bot.domain.health import classify_events, compute_health_diff
from sentinel_bot.domain.models import (
    ALERTING,
    EVENT_OVERHEAT_CLEARED,
    EVENT_OVERHEAT_STARTED,
    OK,
    KnownState,
)

from .conftest import BASE_TIME, cpu_policy, make_reading


def _drive(temps, cp=None):
    """Прогнать последовательность температур через reconciliation, имитируя тики."""
    cp = cp or cpu_policy()
    known: dict[str, KnownState] = {}
    transitions = []
    final_state = None
    for i, temp in enumerate(temps):
        now = BASE_TIME + timedelta(minutes=i)
        reading = make_reading(temp, at=now)
        diff = compute_health_diff([reading], known, lambda r: cp, now)
        final_state = diff.states[0]
        known = {
            final_state.component_id: KnownState(
                final_state.component_id,
                final_state.status,
                final_state.consecutive_count,
                final_state.alerting_since,
            )
        }
        transitions.extend((t.from_status, t.to_status) for t in diff.transitions)
    return final_state, transitions


def test_overheat_started_after_n_consecutive():
    state, transitions = _drive([85, 85, 85])
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING
    assert state.alerting_since is not None


def test_below_threshold_count_does_not_alert():
    # Только 2 подряд при пороге 3 — транзиентный всплеск не виден.
    state, transitions = _drive([85, 85])
    assert transitions == []
    assert state.status == OK


def test_debounce_resets_on_interruption():
    # 85,85,(70 сброс),85,85,85 → alert только на последнем третьем подряд.
    state, transitions = _drive([85, 85, 70, 85, 85, 85])
    assert transitions == [(OK, ALERTING)]


def test_overheat_cleared_after_n_consecutive_below_hysteresis():
    # Перегрев, затем 3 подряд ниже warn-delta (75).
    state, transitions = _drive([85, 85, 85, 70, 70, 70])
    assert transitions == [(OK, ALERTING), (ALERTING, OK)]
    assert state.status == OK
    assert state.alerting_since is None


def test_hysteresis_deadband_does_not_clear():
    # После перегрева температура в мёртвой зоне (77) не сбрасывает alert.
    state, transitions = _drive([85, 85, 85, 77, 77, 77, 77])
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING


def test_idempotent_repeated_diff_no_new_transition():
    # Достигли alerting; ещё горячо — повторный прогон не плодит переходы.
    state, transitions = _drive([85, 85, 85, 85, 85])
    assert transitions == [(OK, ALERTING)]


def test_classify_events_maps_transitions():
    _, _ = _drive([85, 85, 85])
    cp = cpu_policy()
    known = {}
    now = BASE_TIME
    # Один прогон до alerting через прямую подачу known.
    known = {"cpu:pkg": KnownState("cpu:pkg", OK, 2, None)}
    diff = compute_health_diff([make_reading(85, at=now)], known, lambda r: cp, now)
    events = classify_events(diff.transitions)
    assert len(events) == 1
    assert events[0].type == EVENT_OVERHEAT_STARTED

    known = {"cpu:pkg": KnownState("cpu:pkg", ALERTING, 2, now)}
    diff = compute_health_diff([make_reading(70, at=now)], known, lambda r: cp, now)
    events = classify_events(diff.transitions)
    assert events[0].type == EVENT_OVERHEAT_CLEARED
