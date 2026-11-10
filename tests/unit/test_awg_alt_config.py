from __future__ import annotations

import tomllib

import pytest

from sa_home_bot.config import AwgAltConfig, VpnConfig
from sa_home_bot.vpn.awg_conf import render


def _base_vpn_cfg(**overrides: object) -> VpnConfig:
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


def test_awg_alt_config_defaults() -> None:
    cfg = AwgAltConfig()
    assert cfg.interface == "awg1"
    assert cfg.port == 51821
    assert cfg.subnet == "10.9.1.0/24"
    assert cfg.jc == 5
    assert cfg.jmin == 40
    assert cfg.jmax == 70
    assert cfg.s1 == 50
    assert cfg.s2 == 80
    assert cfg.s3 == 0
    assert cfg.s4 == 0
    assert cfg.h1 == 5
    assert cfg.h2 == 6
    assert cfg.h3 == 7
    assert cfg.h4 == 8
    assert cfg.i1 == ""
    assert cfg.i2 == ""
    assert cfg.i3 == ""
    assert cfg.i4 == ""
    assert cfg.i5 == ""


def test_awg_alt_h_range_valid() -> None:
    cfg = AwgAltConfig(h1="100-200", h2="1-1", h3=5, h4="10-10")
    assert cfg.h1 == "100-200"
    assert cfg.h2 == "1-1"
    assert cfg.h3 == 5
    assert cfg.h4 == "10-10"


def test_awg_alt_h_range_invalid_format() -> None:
    with pytest.raises(ValueError, match="h1-h4: строка должна быть вида 'a-b'"):
        AwgAltConfig(h1="abc")


def test_awg_alt_h_range_invalid_numbers() -> None:
    with pytest.raises(ValueError, match="h1-h4: границы диапазона должны быть целыми"):
        AwgAltConfig(h1="a-b")


def test_awg_alt_h_range_negative() -> None:
    with pytest.raises(ValueError, match="h1-h4: границы диапазона должны быть >= 1"):
        AwgAltConfig(h1="0-10")


def test_awg_alt_h_range_left_greater() -> None:
    with pytest.raises(ValueError, match="h1-h4: левая граница не может быть больше правой"):
        AwgAltConfig(h1="200-100")


def test_vpn_config_without_awg_alt() -> None:
    cfg = _base_vpn_cfg()
    assert cfg.awg_alt is None
    assert cfg.awg_alt_render_cfg() is None
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


def test_vpn_config_with_awg_alt() -> None:
    cfg = _base_vpn_cfg(
        awg_alt={
            "interface": "awg1",
            "port": 51821,
            "subnet": "10.9.1.0/24",
            "jc": 3,
            "jmin": 30,
            "jmax": 60,
            "s1": 40,
            "s2": 70,
            "s3": 10,
            "s4": 20,
            "h1": "100-200",
            "h2": 6,
            "h3": 7,
            "h4": 8,
            "i1": "<b 0x1234><r 16><c><t>",
            "i2": "",
            "i3": "<b 0x5678><r 16><c><t>",
            "i4": "",
            "i5": "",
        }
    )
    assert cfg.awg_alt is not None
    assert cfg.awg_alt.interface == "awg1"
    assert cfg.awg_alt.port == 51821
    assert cfg.awg_alt.subnet == "10.9.1.0/24"

    alt_cfg = cfg.awg_alt_render_cfg()
    assert alt_cfg is not None
    assert alt_cfg.interface == "awg1"
    assert alt_cfg.subnet == "10.9.1.0/24"
    assert alt_cfg.endpoint_port == 51821
    assert alt_cfg.endpoint_host == "203.0.113.7"
    assert alt_cfg.dns == "1.1.1.1"
    assert alt_cfg.mtu == 1280
    assert alt_cfg.jc == 3
    assert alt_cfg.jmin == 30
    assert alt_cfg.jmax == 60
    assert alt_cfg.s1 == 40
    assert alt_cfg.s2 == 70
    assert alt_cfg.s3 == 10
    assert alt_cfg.s4 == 20
    assert alt_cfg.h1 == "100-200"
    assert alt_cfg.h2 == 6
    assert alt_cfg.h3 == 7
    assert alt_cfg.h4 == 8
    assert alt_cfg.i1 == "<b 0x1234><r 16><c><t>"
    assert alt_cfg.i2 == ""
    assert alt_cfg.i3 == "<b 0x5678><r 16><c><t>"
    assert alt_cfg.i4 == ""
    assert alt_cfg.i5 == ""
    assert alt_cfg.awg2 is True
    assert alt_cfg.awg_alt is None

    out = render(alt_cfg, "PRIVKEY", "10.9.1.4", "SRVPUB")
    assert "Endpoint = 203.0.113.7:51821" in out
    assert "Jc = 3" in out
    assert "Jmin = 30" in out
    assert "Jmax = 60" in out
    assert "S1 = 40" in out
    assert "S2 = 70" in out
    assert "S3 = 10" in out
    assert "S4 = 20" in out
    assert "H1 = 100-200" in out
    assert "I1 = <b 0x1234><r 16><c><t>" in out
    assert "I3 = <b 0x5678><r 16><c><t>" in out
    assert "I2 =" not in out
    assert "I4 =" not in out
    assert "I5 =" not in out


@pytest.mark.parametrize(
    "alt_data,match",
    [
        ({"interface": "awg0"}, "awg_alt.interface не должен совпадать с interface"),
        ({"port": 51820}, "awg_alt.port не должен совпадать с endpoint_port"),
        ({"subnet": "10.9.0.0/24"}, "awg_alt.subnet не должен пересекаться с subnet"),
    ],
)
def test_vpn_config_awg_alt_validation_errors(alt_data: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _base_vpn_cfg(awg_alt=alt_data)


def test_awg_alt_from_toml() -> None:
    toml_str = """
    [vpn]
    endpoint_host = "203.0.113.7"
    endpoint_port = 51820
    [vpn.awg_alt]
    interface = "awg1"
    port = 51821
    subnet = "10.9.1.0/24"
    jc = 3
    jmin = 30
    jmax = 60
    s1 = 40
    s2 = 70
    s3 = 10
    s4 = 20
    h1 = "100-200"
    i1 = "<b 0x1234><r 16><c><t>"
    i3 = "<b 0x5678><r 16><c><t>"
    """
    data = tomllib.loads(toml_str)
    cfg = VpnConfig(**data["vpn"])
    assert cfg.awg_alt is not None
    assert cfg.awg_alt.interface == "awg1"
    assert cfg.awg_alt.port == 51821
    assert cfg.awg_alt.subnet == "10.9.1.0/24"
    assert cfg.awg_alt.jc == 3
    assert cfg.awg_alt.jmin == 30
    assert cfg.awg_alt.jmax == 60
    assert cfg.awg_alt.s1 == 40
    assert cfg.awg_alt.s2 == 70
    assert cfg.awg_alt.s3 == 10
    assert cfg.awg_alt.s4 == 20
    assert cfg.awg_alt.h1 == "100-200"
    assert cfg.awg_alt.i1 == "<b 0x1234><r 16><c><t>"
    assert cfg.awg_alt.i3 == "<b 0x5678><r 16><c><t>"
