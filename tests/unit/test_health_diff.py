from datetime import timedelta

from sa_home_bot.domain.health import compute_health_diff
from sa_home_bot.domain.models import (
    ALERTING,
    OK,
    KnownState,
)

from .conftest import BASE_TIME, cpu_policy, make_reading


def _drive(temps, cp=None):
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
    state, transitions = _drive([85, 85])
    assert transitions == []
    assert state.status == OK


def test_debounce_resets_on_interruption():
    state, transitions = _drive([85, 85, 70, 85, 85, 85])
    assert transitions == [(OK, ALERTING)]


def test_overheat_cleared_after_n_consecutive_below_hysteresis():
    state, transitions = _drive([85, 85, 85, 70, 70, 70])
    assert transitions == [(OK, ALERTING), (ALERTING, OK)]
    assert state.status == OK
    assert state.alerting_since is None


def test_hysteresis_deadband_does_not_clear():
    state, transitions = _drive([85, 85, 85, 77, 77, 77, 77])
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING


def test_idempotent_repeated_diff_no_new_transition():
    state, transitions = _drive([85, 85, 85, 85, 85])
    assert transitions == [(OK, ALERTING)]
