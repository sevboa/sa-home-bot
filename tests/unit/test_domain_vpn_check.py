from datetime import UTC, datetime, timedelta

import pytest

from sa_home_bot.domain.vpn_check import (
    ALERTING,
    OK,
    PARTIAL,
    CheckResult,
    KnownCheckState,
    reconcile_vpn_check,
    rollup_status,
)

BASE_TIME = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
FAIL_THRESHOLD = 2
CLEAR_THRESHOLD = 1


def _drive(oks: list[bool], *, fail_threshold=FAIL_THRESHOLD, clear_threshold=CLEAR_THRESHOLD):
    """Прогнать последовательность ok/fail через reconciliation, имитируя тики."""
    known: KnownCheckState | None = None
    transitions = []
    final_state = None
    for i, ok in enumerate(oks):
        now = BASE_TIME + timedelta(minutes=i)
        result = CheckResult(
            node="jeeves",
            server="jeeves",
            transport="awg",
            target="https://1.1.1.1",
            ok=ok,
            latency_ms=10,
            error=None,
        )
        state, transition = reconcile_vpn_check(
            result, known, now, fail_threshold=fail_threshold, clear_threshold=clear_threshold
        )
        final_state = state
        known = KnownCheckState(
            status=state.status,
            consecutive_count=state.consecutive_count,
            alerting_since=state.alerting_since,
        )
        if transition is not None:
            transitions.append((transition.from_status, transition.to_status))
    return final_state, transitions


def test_alert_after_n_consecutive_failures():
    state, transitions = _drive([False, False])
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING
    assert state.alerting_since is not None


def test_single_failure_does_not_alert():
    state, transitions = _drive([False])
    assert transitions == []
    assert state.status == OK


def test_debounce_resets_on_interruption():
    # fail, ok (сброс), fail, fail → alert только на последней паре подряд.
    state, transitions = _drive([False, True, False, False])
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING


def test_recovers_after_clear_threshold():
    state, transitions = _drive([False, False, True])
    assert transitions == [(OK, ALERTING), (ALERTING, OK)]
    assert state.status == OK
    assert state.alerting_since is None


def test_higher_clear_threshold_requires_multiple_successes():
    state, transitions = _drive([False, False, True], clear_threshold=2)
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING


def test_no_repeated_alert_while_still_failing():
    # Мут: после первого перехода в alerting дальнейшие неудачи не дают
    # новых транзишенов (событие не эмитится повторно на каждый тик).
    state, transitions = _drive([False, False, False, False, False])
    assert transitions == [(OK, ALERTING)]
    assert state.status == ALERTING


def test_first_result_unknown_state_ok_baseline():
    result = CheckResult(
        node="alfred",
        server="jeeves",
        transport="reality",
        target="https://api.telegram.org",
        ok=True,
        latency_ms=5,
        error=None,
    )
    state, transition = reconcile_vpn_check(
        result, None, BASE_TIME, fail_threshold=FAIL_THRESHOLD, clear_threshold=CLEAR_THRESHOLD
    )
    assert transition is None
    assert state.status == OK
    assert state.last_ok is True


def test_state_and_transition_carry_server_and_transport():
    result = CheckResult(
        node="alfred",
        server="wooster",
        transport="reality",
        target="https://1.1.1.1",
        ok=False,
        latency_ms=None,
        error="timeout",
    )
    state, _ = reconcile_vpn_check(result, None, BASE_TIME, fail_threshold=1, clear_threshold=1)
    _, transition = reconcile_vpn_check(
        result,
        KnownCheckState(status=OK, consecutive_count=0, alerting_since=None),
        BASE_TIME,
        fail_threshold=1,
        clear_threshold=1,
    )
    assert state.server == "wooster"
    assert state.transport == "reality"
    assert transition is not None
    assert transition.server == "wooster"
    assert transition.transport == "reality"


def test_rollup_all_ok():
    assert rollup_status([OK, OK, OK]) == OK


def test_rollup_all_alerting():
    assert rollup_status([ALERTING, ALERTING]) == ALERTING


def test_rollup_mixed_is_partial():
    assert rollup_status([OK, ALERTING]) == PARTIAL


def test_rollup_single_observer():
    assert rollup_status([OK]) == OK
    assert rollup_status([ALERTING]) == ALERTING


def test_rollup_empty_raises():
    with pytest.raises(ValueError):
        rollup_status([])
