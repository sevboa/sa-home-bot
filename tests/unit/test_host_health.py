"""domain/host.py: полосы порогов по направлению + reconcile с гистерезисом."""

from datetime import UTC, datetime, timedelta

from sa_home_bot.domain.host import (
    METRICS,
    HostMetricPolicy,
    HostMetricReading,
    compute_host_diff,
)
from sa_home_bot.domain.models import ALERTING, OK, KnownState
from sa_home_bot.domain.policy import BAND_MID, BAND_OVER, BAND_UNDER

BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_band_direction_above_steal():
    spec = METRICS["steal_pct"]  # warn 10, hysteresis 3
    assert spec.band(12.0) == BAND_OVER
    assert spec.band(6.0) == BAND_UNDER
    assert spec.band(8.5) == BAND_MID


def test_band_direction_below_mem_available():
    spec = METRICS["mem_available_pct"]  # warn 15, hysteresis 4, DIR_BELOW
    assert spec.band(10.0) == BAND_OVER  # мало памяти — тревога
    assert spec.band(20.0) == BAND_UNDER  # памяти снова много — норма
    assert spec.band(17.0) == BAND_MID


def _reading(metric: str, value: float, at: datetime) -> HostMetricReading:
    spec = METRICS[metric]
    return HostMetricReading(f"host:{metric}", metric, spec.label, value, spec.unit, at)


def _drive(metric: str, values: list[float], to_alert=2, to_clear=2):
    known: dict[str, KnownState] = {}
    transitions: list[tuple[str, str]] = []
    state = None
    resolver = lambda r: HostMetricPolicy(METRICS[r.metric], to_alert, to_clear)  # noqa: E731
    for i, v in enumerate(values):
        now = BASE + timedelta(minutes=i)
        diff = compute_host_diff([_reading(metric, v, now)], known, resolver, now)
        state = diff.states[0]
        known = {
            state.component_id: KnownState(
                state.component_id, state.status, state.consecutive_count, state.alerting_since
            )
        }
        transitions += [(t.from_status, t.to_status) for t in diff.transitions]
    return state, transitions


def test_steal_alerts_after_two_consecutive_over():
    state, tr = _drive("steal_pct", [30, 30])
    assert state.status == ALERTING
    assert tr == [(OK, ALERTING)]
    assert state.alerting_since is not None


def test_steal_transient_spike_does_not_alert():
    state, tr = _drive("steal_pct", [30, 2, 30])
    assert state.status == OK
    assert tr == []


def test_mem_available_alerts_on_drop_and_clears_on_recovery():
    state, tr = _drive("mem_available_pct", [10, 10, 30, 30])
    assert tr == [(OK, ALERTING), (ALERTING, OK)]
    assert state.status == OK
