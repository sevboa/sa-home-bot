from __future__ import annotations

import pytest

from sa_home_bot.config import RealityAltConfig, RealityTransportConfig
from sa_home_bot.reality.client_config import RealityParams


def test_reality_alt_config_defaults():
    cfg = RealityAltConfig(sni="example.com")
    assert cfg.port == 2083
    assert cfg.sni == "example.com"
    assert cfg.dest == ""
    assert cfg.inbound_tag == "reality-alt"
    assert cfg.effective_dest == "example.com:443"


def test_reality_alt_config_with_dest():
    cfg = RealityAltConfig(sni="example.com", dest="custom:8443")
    assert cfg.effective_dest == "custom:8443"


def test_reality_transport_config_alt_none_by_default():
    cfg = RealityTransportConfig()
    assert cfg.alt is None


def test_reality_transport_config_alt_parses_section():
    cfg = RealityTransportConfig(
        port=8443,
        sni="main.com",
        alt={"sni": "alt.com", "port": 2083},
    )
    assert cfg.alt is not None
    assert cfg.alt.sni == "alt.com"
    assert cfg.alt.port == 2083
    assert cfg.alt.inbound_tag == "reality-alt"


def test_reality_transport_config_rejects_same_port():
    with pytest.raises(ValueError, match="alt.port не должен совпадать с port"):
        RealityTransportConfig(
            port=8443,
            sni="main.com",
            alt={"sni": "alt.com", "port": 8443},
        )


def test_reality_transport_config_rejects_same_inbound_tag():
    with pytest.raises(ValueError, match="alt.inbound_tag не должен совпадать с inbound_tag"):
        RealityTransportConfig(
            port=8443,
            sni="main.com",
            alt={"sni": "alt.com", "port": 2083, "inbound_tag": "reality-in"},
        )


def test_alt_from_config_returns_none_when_no_alt():
    cfg = RealityTransportConfig(port=8443, sni="main.com")
    assert RealityParams.alt_from_config(cfg) is None


def test_alt_from_config_returns_params_when_alt_present():
    cfg = RealityTransportConfig(
        endpoint_host="1.2.3.4",
        port=8443,
        server_public_key="PUBKEY",
        short_id="abcd",
        sni="main.com",
        flow="xtls-rprx-vision",
        fingerprint="chrome",
        alt={"sni": "alt.com", "port": 2083},
    )
    params = RealityParams.alt_from_config(cfg)
    assert params is not None
    assert params.endpoint_host == "1.2.3.4"
    assert params.port == 2083
    assert params.server_public_key == "PUBKEY"
    assert params.short_id == "abcd"
    assert params.sni == "alt.com"
    assert params.flow == "xtls-rprx-vision"
    assert params.fingerprint == "chrome"


def test_from_config_returns_original_port_and_sni_when_alt_present():
    cfg = RealityTransportConfig(
        endpoint_host="1.2.3.4",
        port=8443,
        server_public_key="PUBKEY",
        short_id="abcd",
        sni="main.com",
        flow="xtls-rprx-vision",
        fingerprint="chrome",
        alt={"sni": "alt.com", "port": 2083},
    )
    params = RealityParams.from_config(cfg)
    assert params.port == 8443
    assert params.sni == "main.com"
