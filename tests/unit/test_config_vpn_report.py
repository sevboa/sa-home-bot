from __future__ import annotations

import pytest

from sa_home_bot.config import Settings, VpnReportConfig


def test_vpn_report_defaults():
    cfg = VpnReportConfig()
    assert cfg.enabled is False
    assert cfg.time == "09:00"
    assert cfg.tz == "Asia/Almaty"
    assert cfg.hours == 24
    assert cfg.bucket_min == 15
    assert cfg.llm_timeout_s == 90.0
    assert Settings().vpn_report == cfg


@pytest.mark.parametrize("bad_time", ["25:00", "9", "ab:cd", "09:60", "09:0", "9:05"])
def test_vpn_report_rejects_bad_time(bad_time):
    with pytest.raises(ValueError):
        VpnReportConfig(time=bad_time)
