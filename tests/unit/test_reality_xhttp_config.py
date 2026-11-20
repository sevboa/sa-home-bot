from __future__ import annotations

import pytest

from sa_home_bot.config import RealityTransportConfig, RealityXhttpConfig


def test_reality_xhttp_config_defaults():
    cfg = RealityXhttpConfig(sni="example.com", path="/ring")
    assert cfg.port == 2087
    assert cfg.sni == "example.com"
    assert cfg.dest == ""
    assert cfg.inbound_tag == "reality-xhttp"
    assert cfg.mode == "auto"
    assert cfg.effective_dest == "example.com:443"


def test_reality_xhttp_config_with_dest_and_mode():
    cfg = RealityXhttpConfig(
        sni="example.com",
        dest="custom:8443",
        path="/ring",
        mode="stream-up",
    )
    assert cfg.effective_dest == "custom:8443"
    assert cfg.mode == "stream-up"


def test_reality_xhttp_config_rejects_bad_path():
    for bad in ("ring", "/"):
        with pytest.raises(ValueError, match="path: строка должна начинаться с '/'"):
            RealityXhttpConfig(sni="example.com", path=bad)


def test_reality_xhttp_config_rejects_bad_mode():
    with pytest.raises(ValueError, match="mode: должно быть"):
        RealityXhttpConfig(sni="example.com", path="/ring", mode="websocket")


def test_reality_transport_config_xhttp_none_by_default():
    cfg = RealityTransportConfig()
    assert cfg.xhttp is None


def test_reality_transport_config_xhttp_parses_section():
    cfg = RealityTransportConfig(
        port=8443,
        sni="main.com",
        xhttp={"sni": "xh.com", "port": 2087, "path": "/ring"},
    )
    assert cfg.xhttp is not None
    assert cfg.xhttp.sni == "xh.com"
    assert cfg.xhttp.port == 2087
    assert cfg.xhttp.inbound_tag == "reality-xhttp"
    assert cfg.xhttp.mode == "auto"


def test_reality_transport_config_rejects_xhttp_same_port_as_main():
    with pytest.raises(ValueError, match="xhttp.port не должен совпадать с port"):
        RealityTransportConfig(
            port=8443,
            sni="main.com",
            xhttp={"sni": "xh.com", "port": 8443, "path": "/ring"},
        )


def test_reality_transport_config_rejects_xhttp_same_tag_as_main():
    with pytest.raises(ValueError, match="xhttp.inbound_tag не должен совпадать с inbound_tag"):
        RealityTransportConfig(
            port=8443,
            sni="main.com",
            xhttp={"sni": "xh.com", "port": 2087, "path": "/ring", "inbound_tag": "reality-in"},
        )


def test_reality_transport_config_rejects_xhttp_same_port_as_alt():
    with pytest.raises(ValueError, match="xhttp.port не должен совпадать с alt.port"):
        RealityTransportConfig(
            port=8443,
            sni="main.com",
            alt={"sni": "alt.com", "port": 2083},
            xhttp={"sni": "xh.com", "port": 2083, "path": "/ring"},
        )


def test_reality_transport_config_rejects_xhttp_same_tag_as_alt():
    with pytest.raises(ValueError, match="xhttp.inbound_tag не должен совпадать с alt.inbound_tag"):
        RealityTransportConfig(
            port=8443,
            sni="main.com",
            alt={"sni": "alt.com", "port": 2083},
            xhttp={"sni": "xh.com", "port": 2087, "path": "/ring", "inbound_tag": "reality-alt"},
        )
