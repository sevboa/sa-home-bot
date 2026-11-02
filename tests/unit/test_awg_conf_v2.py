from __future__ import annotations

import pytest

from sa_home_bot.config import VpnConfig
from sa_home_bot.vpn.awg_conf import AWG2_EXTRA_KEYS, ISSUED_OBFUSCATION_KEYS, issued_keys, render


def _base_cfg(**overrides: object) -> VpnConfig:
    data = {
        "endpoint_host": "203.0.113.7",
        "endpoint_port": 51820,
        "dns": "1.1.1.1",
        "mtu": 1280,
        "jc": 5,
        "jmin": 40,
        "jmax": 70,
        "s1": 50,
        "s2": 80,
        "s3": 0,
        "s4": 0,
        "h1": 5,
        "h2": 6,
        "h3": 7,
        "h4": 8,
        "i1": "",
        "i2": "",
        "i3": "",
        "i4": "",
        "i5": "",
        "awg2": False,
    }
    data.update(overrides)
    return VpnConfig(**data)


def test_issued_keys_without_awg2() -> None:
    cfg = _base_cfg()
    assert issued_keys(cfg) == ISSUED_OBFUSCATION_KEYS


def test_issued_keys_with_awg2_no_i_values() -> None:
    cfg = _base_cfg(awg2=True)
    expected = ISSUED_OBFUSCATION_KEYS + ("S3", "S4")
    assert issued_keys(cfg) == expected


def test_issued_keys_with_awg2_some_i_values() -> None:
    cfg = _base_cfg(awg2=True, i1="<b 0x1234><r 16><c><t>", i3="<b 0x5678><r 16><c><t>")
    expected = ISSUED_OBFUSCATION_KEYS + ("S3", "S4", "I1", "I3")
    assert issued_keys(cfg) == expected


def test_render_awg2_false_byte_for_byte() -> None:
    cfg = _base_cfg()
    out = render(cfg, "PRIVKEY", "10.9.0.4", "SRVPUB")
    expected = (
        "[Interface]\n"
        "PrivateKey = PRIVKEY\n"
        "Address = 10.9.0.4/32\n"
        "DNS = 1.1.1.1\n"
        "MTU = 1280\n"
        "Jc = 5\n"
        "Jmin = 40\n"
        "Jmax = 70\n"
        "S1 = 50\n"
        "S2 = 80\n"
        "H1 = 5\n"
        "H2 = 6\n"
        "H3 = 7\n"
        "H4 = 8\n"
        "\n"
        "[Peer]\n"
        "PublicKey = SRVPUB\n"
        "Endpoint = 203.0.113.7:51820\n"
        "AllowedIPs = 0.0.0.0/0\n"
        "PersistentKeepalive = 25\n"
    )
    assert out == expected


def test_render_awg2_true_includes_s3_s4_and_nonempty_i() -> None:
    cfg = _base_cfg(
        awg2=True,
        s3=10,
        s4=20,
        i1="<b 0x1234><r 16><c><t>",
        i2="",
        i3="<b 0x5678><r 16><c><t>",
        i4="",
        i5="",
        h1="100-200",
    )
    out = render(cfg, "PRIVKEY", "10.9.0.4", "SRVPUB")
    assert "S3 = 10" in out
    assert "S4 = 20" in out
    assert "I1 = <b 0x1234><r 16><c><t>" in out
    assert "I3 = <b 0x5678><r 16><c><t>" in out
    assert "I2 =" not in out
    assert "I4 =" not in out
    assert "I5 =" not in out
    assert "H1 = 100-200" in out


def test_vpn_config_h_range_valid() -> None:
    cfg = _base_cfg(h1="100-200", h2="1-1", h3=5, h4="10-10")
    assert cfg.h1 == "100-200"
    assert cfg.h2 == "1-1"
    assert cfg.h3 == 5
    assert cfg.h4 == "10-10"


def test_vpn_config_h_range_invalid_format() -> None:
    with pytest.raises(ValueError, match="h1-h4: строка должна быть вида 'a-b'"):
        _base_cfg(h1="abc")


def test_vpn_config_h_range_invalid_numbers() -> None:
    with pytest.raises(ValueError, match="h1-h4: границы диапазона должны быть целыми"):
        _base_cfg(h1="a-b")


def test_vpn_config_h_range_negative() -> None:
    with pytest.raises(ValueError, match="h1-h4: границы диапазона должны быть >= 1"):
        _base_cfg(h1="0-10")


def test_vpn_config_h_range_left_greater() -> None:
    with pytest.raises(ValueError, match="h1-h4: левая граница не может быть больше правой"):
        _base_cfg(h1="9-3")


def test_awg2_extra_keys_constant() -> None:
    assert AWG2_EXTRA_KEYS == ("S3", "S4", "I1", "I2", "I3", "I4", "I5")
