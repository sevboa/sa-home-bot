from __future__ import annotations

import json
from dataclasses import replace

import pytest

from sa_home_bot.config import RealityAltConfig, RealityTransportConfig, RealityXhttpConfig
from sa_home_bot.reality.client_config import (
    DEFAULT_FLOW,
    RealityParams,
    parse_vless_url,
    probe_entry_name,
    render_singbox_config,
    render_singbox_multi,
    render_vless_url,
    short_domain,
)

UUID = "11111111-2222-3333-4444-555555555555"

CFG = RealityTransportConfig(
    endpoint_host="198.51.100.1",
    server_public_key="PUB1",
    short_id="abcd1234",
    sni="www.google.com",
    xhttp=RealityXhttpConfig(
        port=2087, sni="www.microsoft.com", path="/long-path", mode="stream-up"
    ),
)


def test_xhttp_from_config_takes_port_sni_path_mode() -> None:
    params = RealityParams.xhttp_from_config(CFG)
    assert params is not None
    assert params.endpoint_host == "198.51.100.1"
    assert params.port == 2087
    assert params.server_public_key == "PUB1"
    assert params.short_id == "abcd1234"
    assert params.sni == "www.microsoft.com"
    assert params.flow == ""
    assert params.network == "xhttp"
    assert params.xhttp_path == "/long-path"
    assert params.xhttp_mode == "stream-up"


def test_xhttp_from_config_without_section_is_none() -> None:
    cfg = RealityTransportConfig(endpoint_host="h", server_public_key="k", short_id="s")
    assert RealityParams.xhttp_from_config(cfg) is None


def test_render_vless_url_tcp_is_unchanged() -> None:
    params = RealityParams("198.51.100.1", 8443, "PUB1", "abcd1234", "www.google.com")
    url = render_vless_url(params, UUID, "label")
    assert "type=tcp" in url
    assert "flow=xtls-rprx-vision" in url
    assert "path" not in url
    assert "mode" not in url


def test_render_vless_url_xhttp_shape() -> None:
    params = RealityParams.xhttp_from_config(CFG)
    assert params is not None
    url = render_vless_url(params, UUID, "lab/el")
    assert url.startswith(f"vless://{UUID}@198.51.100.1:2087?")
    assert "encryption=none" in url
    assert "security=reality" in url
    assert "sni=www.microsoft.com" in url
    assert "fp=chrome" in url
    assert "pbk=PUB1" in url
    assert "sid=abcd1234" in url
    assert "type=xhttp" in url
    assert "path=%2Flong-path" in url
    assert "mode=stream-up" in url
    assert "flow" not in url
    assert url.endswith("#lab%2Fel")


def test_render_vless_url_xhttp_omits_empty_flow_only() -> None:
    params = RealityParams.xhttp_from_config(CFG)
    assert params is not None
    url = render_vless_url(params, UUID, "x")
    assert "flow=" not in url


def test_render_parse_roundtrip_tcp() -> None:
    params = RealityParams("198.51.100.1", 8443, "PUB1", "abcd1234", "www.google.com")
    url = render_vless_url(params, UUID, "label")
    assert parse_vless_url(url) == (params, UUID)


def test_render_parse_roundtrip_xhttp() -> None:
    params = RealityParams.xhttp_from_config(CFG)
    assert params is not None
    url = render_vless_url(params, UUID, "🇳🇱 Rose")
    parsed, client_uuid = parse_vless_url(url)
    assert client_uuid == UUID
    assert parsed == params


def test_parse_vless_url_xhttp_defaults() -> None:
    url = (
        "vless://uuid-1@1.2.3.4:2087?encryption=none&security=reality"
        "&sni=www.microsoft.com&fp=chrome&pbk=PBK&sid=SID&type=xhttp"
        "&path=%2Fp%2Fath&mode=packet-up#label"
    )
    params, client_uuid = parse_vless_url(url)
    assert client_uuid == "uuid-1"
    assert params.network == "xhttp"
    assert params.flow == ""
    assert params.xhttp_path == "/p/ath"
    assert params.xhttp_mode == "packet-up"


def test_parse_vless_url_without_type_is_tcp() -> None:
    url = (
        "vless://uuid-1@1.2.3.4:8443?encryption=none&flow=xtls-rprx-vision"
        "&security=reality&sni=www.google.com&fp=chrome&pbk=PBK&sid=SID#label"
    )
    params, client_uuid = parse_vless_url(url)
    assert client_uuid == "uuid-1"
    assert params.network == "tcp"
    assert params.flow == DEFAULT_FLOW
    assert params.xhttp_path == ""


def _xhttp_params(mode: str) -> RealityParams:
    params = RealityParams.xhttp_from_config(CFG)
    assert params is not None
    return replace(params, xhttp_mode=mode)


def _tcp_params() -> RealityParams:
    return RealityParams.from_config(CFG)


@pytest.mark.parametrize("mode", ["auto", "stream-up", "packet-up"])
@pytest.mark.parametrize("multi", [False, True])
def test_singbox_xhttp_outbound_is_packet_up_without_flow(mode: str, multi: bool) -> None:
    params = _xhttp_params(mode)
    if multi:
        config = json.loads(render_singbox_multi([("🇺🇸 XHTTP", params, UUID)]))
        outbound = next(o for o in config["outbounds"] if o.get("tag") == "🇺🇸 XHTTP")
    else:
        config = json.loads(render_singbox_config(params, UUID))
        outbound = config["outbounds"][0]
    assert "flow" not in outbound
    assert outbound["server_port"] == 2087
    assert outbound["tls"]["reality"]["enabled"] is True
    assert outbound["transport"] == {
        "type": "xhttp",
        "path": "/long-path",
        "mode": "packet-up",
        "x_padding_bytes": "100-1000",
    }


def test_singbox_tcp_outbound_keeps_flow_without_transport() -> None:
    config = json.loads(render_singbox_multi([("tcp", _tcp_params(), UUID)]))
    outbound = config["outbounds"][0]
    assert outbound["flow"] == DEFAULT_FLOW
    assert "transport" not in outbound


@pytest.mark.parametrize("group", ["auto", "proxy"])
def test_singbox_multi_groups_include_xhttp_tag(group: str) -> None:
    servers = [("🇺🇸 Reality", _tcp_params(), UUID), ("🇺🇸 XHTTP", _xhttp_params("auto"), UUID)]
    config = json.loads(render_singbox_multi(servers))
    members = next(o for o in config["outbounds"] if o.get("tag") == group)["outbounds"]
    assert "🇺🇸 XHTTP" in members


@pytest.mark.parametrize(
    ("sni", "expected"),
    [
        ("www.google.com", "google"),
        ("azgez.com", "azgez"),
        ("feedwonk.com", "feedwonk"),
        ("localhost", "localhost"),
        ("", ""),
    ],
)
def test_short_domain(sni: str, expected: str) -> None:
    assert short_domain(sni) == expected


def test_reality_artifacts_with_xhttp_adds_xhttp_share_url() -> None:
    mixin = _fake_mixin(xhttp=True)
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=123)
    assert "198.51.100.1:2083" in artifacts["share_url"]
    assert "www.cloudflare.com" in artifacts["share_url"]
    assert "198.51.100.1:2083" in artifacts["alt_share_url"]
    assert "198.51.100.1:2087" in artifacts["xhttp_share_url"]
    assert "www.microsoft.com" in artifacts["xhttp_share_url"]
    assert "type=xhttp" in artifacts["xhttp_share_url"]
    assert "path=%2Flong-path" in artifacts["xhttp_share_url"]
    assert "%C2%B7%20XHTTP" in artifacts["xhttp_share_url"]
    assert "flow" not in artifacts["xhttp_share_url"]


def test_reality_artifacts_probe_chat_with_xhttp_adds_xhttp_share_url() -> None:
    mixin = _fake_mixin(xhttp=True)
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=PROBE_CHAT_ID)
    assert "198.51.100.1:8443" in artifacts["share_url"]
    assert "www.google.com" in artifacts["share_url"]
    assert "198.51.100.1:2087" in artifacts["xhttp_share_url"]
    assert "type=xhttp" in artifacts["xhttp_share_url"]


def test_reality_artifacts_without_xhttp_has_no_xhttp_share_url() -> None:
    mixin = _fake_mixin(xhttp=False)
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=123)
    assert "198.51.100.1:2083" in artifacts["share_url"]
    assert "xhttp_share_url" not in artifacts
    assert "alt_share_url" in artifacts


def test_reality_artifacts_probe_chat_without_xhttp_has_no_xhttp_share_url() -> None:
    mixin = _fake_mixin(xhttp=False)
    artifacts = mixin._reality_artifacts("uuid-nl", "📱 iPhone", chat_id=PROBE_CHAT_ID)
    assert "198.51.100.1:8443" in artifacts["share_url"]
    assert "xhttp_share_url" not in artifacts


from sa_home_bot.vpn.peers import PeersMixin  # noqa: E402
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID  # noqa: E402


def _fake_mixin(xhttp: bool) -> PeersMixin:
    cfg = RealityTransportConfig(
        endpoint_host="198.51.100.1",
        server_public_key="PUB1",
        short_id="abcd1234",
        sni="www.google.com",
        alt=RealityAltConfig(port=2083, sni="www.cloudflare.com", inbound_tag="reality-alt"),
        xhttp=(
            RealityXhttpConfig(port=2087, sni="www.microsoft.com", path="/long-path")
            if xhttp
            else None
        ),
    )

    class FakeMixin(PeersMixin):
        _node = "jeeves"
        _cfg = type("obj", (), {"location": "🇳🇱 Нидерланды"})()
        _reality_cfg = cfg

        def _reality_params(self):
            return RealityParams.from_config(self._reality_cfg)

        def _reality_alt_params(self):
            return RealityParams.alt_from_config(self._reality_cfg)

        def _reality_xhttp_params(self):
            return RealityParams.xhttp_from_config(self._reality_cfg)

    return FakeMixin()


@pytest.mark.parametrize(
    ("transport", "sni", "expected"),
    [
        ("awg", None, "AWG 1.0"),
        ("awg_alt", "ignored.com", "AWG 2.0"),
        ("reality", "www.google.com", "Reality google"),
        ("reality_alt", "azgez.com", "Reality azgez"),
        ("reality_xhttp", "feedwonk.com", "XHTTP feedwonk"),
        ("reality", None, "Reality"),
        ("reality_xhttp", "", "XHTTP"),
    ],
)
def test_probe_entry_name(transport: str, sni: str | None, expected: str) -> None:
    assert probe_entry_name(transport, sni) == expected
