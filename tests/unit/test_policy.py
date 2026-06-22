from sentinel_bot.domain.policy import BAND_MID, BAND_OVER, BAND_UNDER, FixedThresholdPolicy

from .conftest import make_reading

POLICY = FixedThresholdPolicy(warn_c=80.0, crit_c=90.0, hysteresis_delta_c=5.0)


def test_over_at_and_above_warn():
    assert POLICY.band(make_reading(80.0)) == BAND_OVER
    assert POLICY.band(make_reading(95.0)) == BAND_OVER


def test_under_at_or_below_warn_minus_delta():
    assert POLICY.band(make_reading(75.0)) == BAND_UNDER
    assert POLICY.band(make_reading(40.0)) == BAND_UNDER


def test_mid_in_deadband():
    assert POLICY.band(make_reading(77.0)) == BAND_MID
    assert POLICY.band(make_reading(79.9)) == BAND_MID
