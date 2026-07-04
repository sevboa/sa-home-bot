from sa_home_bot.domain.policy import (
    BAND_MID,
    BAND_OVER,
    BAND_UNDER,
    BaselinePolicy,
    BaselineStats,
    FixedThresholdPolicy,
)

from .conftest import make_reading

POLICY = FixedThresholdPolicy(warn_c=80.0, crit_c=90.0, hysteresis_delta_c=5.0)


def _baseline(stats: BaselineStats, warn: float = 80.0) -> BaselinePolicy:
    return BaselinePolicy(
        warn_c=warn,
        crit_c=90.0,
        hysteresis_delta_c=5.0,
        stats=stats,
        min_samples=30,
        k_sigma=4.0,
        min_std_c=3.0,
    )


def test_over_at_and_above_warn():
    assert POLICY.band(make_reading(80.0)) == BAND_OVER
    assert POLICY.band(make_reading(95.0)) == BAND_OVER


def test_under_at_or_below_warn_minus_delta():
    assert POLICY.band(make_reading(75.0)) == BAND_UNDER
    assert POLICY.band(make_reading(40.0)) == BAND_UNDER


def test_mid_in_deadband():
    assert POLICY.band(make_reading(77.0)) == BAND_MID
    assert POLICY.band(make_reading(79.9)) == BAND_MID


# --- BaselinePolicy ---


def test_baseline_cold_start_falls_back_to_fixed_warn():
    # Мало истории (count < min_samples) — работает как FixedThresholdPolicy(warn=80).
    pol = _baseline(BaselineStats(count=5, mean=40.0, std=2.0))
    assert pol.band(make_reading(80.0)) == BAND_OVER
    assert pol.band(make_reading(60.0)) == BAND_UNDER  # <= 80 - 5
    assert pol.band(make_reading(78.0)) == BAND_MID


def test_baseline_warm_lowers_threshold_below_fixed():
    # mean 40, std 2 -> порог = min(80, 40 + 4*max(2,3)) = min(80, 52) = 52.
    pol = _baseline(BaselineStats(count=30, mean=40.0, std=2.0))
    assert pol.band(make_reading(52.0)) == BAND_OVER  # ловим на 52, а fixed молчал бы до 80
    assert pol.band(make_reading(55.0)) == BAND_OVER
    assert pol.band(make_reading(47.0)) == BAND_UNDER  # 52 - 5
    assert pol.band(make_reading(48.0)) == BAND_MID


def test_baseline_never_raises_threshold_above_fixed_warn():
    # Даже если mean высокий, порог не выше warn_c=80 (страховка).
    pol = _baseline(BaselineStats(count=100, mean=90.0, std=10.0))
    assert pol.band(make_reading(80.0)) == BAND_OVER
    assert pol.band(make_reading(74.0)) == BAND_UNDER


def test_baseline_min_std_widens_tight_window():
    # std=0 (константная история) -> берётся min_std_c=3, порог = mean + 12.
    pol = _baseline(BaselineStats(count=50, mean=30.0, std=0.0))
    assert pol.band(make_reading(42.0)) == BAND_OVER  # 30 + 4*3
    assert pol.band(make_reading(41.9)) == BAND_MID
