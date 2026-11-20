from __future__ import annotations

from sa_home_bot.config import RealityAltConfig, RealityTransportConfig, RealityXhttpConfig
from sa_home_bot.reality.xray import DualXrayBackend, RealXrayBackend
from sa_home_bot.vpn.app import _reality_xray_backend


def test_reality_backend_single_without_xhttp() -> None:
    reality = RealityTransportConfig()
    backend = _reality_xray_backend(reality)
    assert isinstance(backend, RealXrayBackend)
    assert backend._tag == "reality-in"
    assert backend._port == 8443


def test_reality_backend_dual_alt_without_xhttp() -> None:
    reality = RealityTransportConfig(alt=RealityAltConfig(sni="a.com"))
    backend = _reality_xray_backend(reality)
    assert isinstance(backend, DualXrayBackend)
    assert isinstance(backend.alt, RealXrayBackend)
    assert backend.alt._tag == "reality-alt"
    assert backend.xhttp is None


def test_reality_backend_xhttp_without_alt() -> None:
    reality = RealityTransportConfig(xhttp=RealityXhttpConfig(sni="b.com", path="/ring"))
    backend = _reality_xray_backend(reality)
    assert isinstance(backend, DualXrayBackend)
    assert backend.alt is None
    assert isinstance(backend.xhttp, RealXrayBackend)
    assert backend.xhttp._tag == "reality-xhttp"
    assert backend.xhttp._port == 2087


def test_reality_backend_xhttp_with_alt() -> None:
    reality = RealityTransportConfig(
        alt=RealityAltConfig(sni="a.com"),
        xhttp=RealityXhttpConfig(sni="b.com", path="/ring", port=2090, inbound_tag="tag-x"),
    )
    backend = _reality_xray_backend(reality)
    assert isinstance(backend, DualXrayBackend)
    assert isinstance(backend.alt, RealXrayBackend)
    assert isinstance(backend.xhttp, RealXrayBackend)
    assert backend.xhttp._tag == "tag-x"
    assert backend.xhttp._port == 2090
