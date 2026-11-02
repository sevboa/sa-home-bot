import pytest

from sa_home_bot.vpn_series.measure import classify

STEADY = [(float(t), t * 100_000) for t in range(0, 31)]
FROZEN_AT_16K = [(0.0, 0), (1.0, 8_000), (2.0, 16_384)] + [(float(t), 16_384) for t in range(3, 30)]
CUT_AT_18K = [(0.0, 0), (1.0, 9_000), (2.0, 18_432)]
SLOW = [(float(t), t * 1_000) for t in range(0, 31)]
LATE_FREEZE = [(float(t), t * 100_000) for t in range(0, 11)] + [
    (float(t), 1_000_000) for t in range(11, 30)
]


@pytest.mark.parametrize(
    ("samples", "curl_exit", "verdict", "stalled_at"),
    [
        (STEADY, 0, "ok", None),
        (STEADY, None, "ok", None),
        (FROZEN_AT_16K, 28, "freeze", 16_384),
        (LATE_FREEZE, None, "freeze", 1_000_000),
        (CUT_AT_18K, 18, "cut", 18_432),
        (CUT_AT_18K, 56, "cut", 18_432),
        (CUT_AT_18K, 7, "error", None),
        ([(0.0, 0), (1.0, 5_000), (2.0, 5_000)], 56, "error", None),
        ([(0.0, 0), (1.0, 500_000), (2.0, 500_000)], 18, "error", None),
        (SLOW, 0, "slow", None),
        ([(0.0, 0), (5.0, 0)], 0, "error", None),
        ([], None, "error", None),
    ],
)
def test_classify_verdict(samples, curl_exit, verdict, stalled_at):
    result = classify(samples, freeze_idle_s=10, min_kbps=100, curl_exit=curl_exit)
    assert result.verdict == verdict
    assert result.stalled_at_bytes == stalled_at


def test_classify_metrics():
    result = classify(STEADY, freeze_idle_s=10, min_kbps=100)
    assert result.bytes_total == 3_000_000
    assert result.duration_s == 30.0
    assert result.max_idle_s == 0.0
    assert result.kbps == pytest.approx(800.0)


def test_classify_measures_idle_from_last_progress():
    result = classify(FROZEN_AT_16K, freeze_idle_s=10, min_kbps=1)
    assert result.max_idle_s == 27.0


def test_classify_short_pause_is_not_freeze():
    samples = [(0.0, 0), (1.0, 50_000), (6.0, 50_000), (7.0, 900_000)]
    assert classify(samples, freeze_idle_s=10, min_kbps=1).verdict == "ok"
