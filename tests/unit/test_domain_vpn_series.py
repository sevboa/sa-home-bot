from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sa_home_bot.domain.models import ALERTING, OK
from sa_home_bot.domain.vpn_check import KnownCheckState
from sa_home_bot.domain.vpn_series import reconcile_vpn_series, series_verdict

NOW = datetime(2026, 11, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "series", "expected"),
    [
        ("ok", ["ok"], "ok"),
        ("ok", ["slow", "ok"], "ok"),
        ("ok", [], "ok"),
        ("tcp_blocked", [], "tcp_blocked"),
        ("proto_blocked", ["freeze"], "proto_blocked"),
        ("ok", ["ok", "freeze", "cut"], "freeze"),
        ("ok", ["slow", "cut"], "cut"),
        ("ok", ["error"], "error"),
    ],
)
def test_series_verdict(raw, series, expected):
    assert series_verdict(raw, series) == expected


def _step(verdict, known):
    return reconcile_vpn_series(verdict, known, NOW, fail_threshold=2, clear_threshold=1)


def test_two_failures_alert_then_one_ok_recovers():
    first = _step("freeze", None)
    assert (first.status, first.transitioned_to) == (OK, None)
    second = _step("freeze", KnownCheckState(first.status, first.consecutive_count, None))
    assert (second.status, second.transitioned_to) == (ALERTING, ALERTING)
    third = _step(
        "ok", KnownCheckState(second.status, second.consecutive_count, second.alerting_since)
    )
    assert (third.status, third.transitioned_to) == (OK, OK)


def test_slow_counts_as_working():
    step = _step(series_verdict("ok", ["slow"]), KnownCheckState(OK, 1, None))
    assert (step.status, step.consecutive_count) == (OK, 0)


def test_failure_at_bytes_takes_first_broken_item():
    from sa_home_bot.domain.vpn_series import failure_at_bytes

    series = [
        {"verdict": "ok", "bytes_total": 9},
        "мусор",
        {"verdict": "freeze", "stalled_at_bytes": 16384, "bytes_total": 16384},
        {"verdict": "cut", "bytes_total": 2048},
    ]
    assert failure_at_bytes(series) == 16384
    assert failure_at_bytes([{"verdict": "cut", "bytes_total": 2048}]) == 2048
    assert failure_at_bytes([{"verdict": "ok"}]) is None
