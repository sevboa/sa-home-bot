from __future__ import annotations

import json

import pytest

from sa_home_bot.reality.client_config import (
    DEFAULT_FLOW,
    RealityParams,
    parse_vless_url,
    render_deep_link,
    render_singbox_config,
    render_vless_url,
)
from sa_home_bot.reality.probe_client_config import render_probe_client_config
from sa_home_bot.reality.routing import DIRECT_SUFFIXES, RULESET_URLS

PARAMS = RealityParams(
    endpoint_host="203.0.113.7",
    port=8443,
    server_public_key="PUBKEYbase64url",
    short_id="0123456789abcdef",
    sni="www.google.com",
)
UUID = "11111111-2222-3333-4444-555555555555"


def _config(**kw) -> dict:
    return json.loads(render_singbox_config(PARAMS, UUID, **kw))


def _rule_set_tags(rule: dict) -> list[str]:
    return list(rule.get("rule_set") or [])


def test_singbox_config_is_valid_json_with_trailing_newline() -> None:
    text = render_singbox_config(PARAMS, UUID)
    assert text.endswith("\n")
    json.loads(text)


def test_proxy_outbound_carries_reality_and_flow() -> None:
    cfg = _config()
    proxy = next(o for o in cfg["outbounds"] if o["tag"] == "proxy")
    assert proxy["type"] == "vless"
    assert proxy["server"] == "203.0.113.7"
    assert proxy["server_port"] == 8443
    assert proxy["uuid"] == UUID
    assert proxy["flow"] == "xtls-rprx-vision"
    assert proxy["tls"]["reality"]["public_key"] == "PUBKEYbase64url"
    assert proxy["tls"]["reality"]["short_id"] == "0123456789abcdef"
    assert proxy["tls"]["server_name"] == "www.google.com"
    assert proxy["tls"]["utls"]["fingerprint"] == "chrome"


def test_route_rules_order_direct_manual_before_blocked_before_inside() -> None:
    rules = _config()["route"]["rules"]
    order: dict[str, int] = {}
    for i, rule in enumerate(rules):
        for tag in _rule_set_tags(rule):
            order.setdefault(tag, i)
    assert order["ru-direct-manual"] < order["ru-blocked"] < order["ru-inside"]


def test_route_rule_actions_match_intent() -> None:
    rules = _config()["route"]["rules"]
    by_tag = {tag: rule for rule in rules for tag in _rule_set_tags(rule)}
    assert by_tag["ru-direct-manual"]["outbound"] == "direct"
    assert by_tag["ru-blocked"]["outbound"] == "proxy"
    assert by_tag["ru-inside"]["outbound"] == "direct"
    assert rules[0].get("action") == "sniff"
    assert any(r.get("action") == "hijack-dns" for r in rules[:3])
    assert any(r.get("ip_is_private") for r in rules)


def test_final_is_proxy_so_new_blocks_are_covered() -> None:
    assert _config()["route"]["final"] == "proxy"


def test_remote_rule_sets_are_binary_srs_from_itdoginfo() -> None:
    rule_sets = {rs["tag"]: rs for rs in _config()["route"]["rule_set"]}
    for tag in ("ru-blocked", "ru-inside"):
        assert rule_sets[tag]["type"] == "remote"
        assert rule_sets[tag]["format"] == "binary"
        assert rule_sets[tag]["url"] == RULESET_URLS[tag]
        assert rule_sets[tag]["download_detour"] == "direct"
    inline = rule_sets["ru-direct-manual"]
    assert inline["type"] == "inline"
    assert inline["rules"][0]["domain_suffix"] == list(DIRECT_SUFFIXES)


def test_dns_blocked_via_tunnel_rest_direct() -> None:
    dns = _config()["dns"]
    tags = {s["tag"]: s for s in dns["servers"]}
    assert tags["proxy-dns"]["detour"] == "proxy"
    assert tags["direct-dns"]["detour"] == "direct"
    assert tags["direct-dns"]["address_resolver"] == "bootstrap-dns"
    assert tags["bootstrap-dns"] == {
        "tag": "bootstrap-dns",
        "address": "77.88.8.8",
        "detour": "direct",
    }
    blocked_rule = next(r for r in dns["rules"] if r.get("rule_set") == ["ru-blocked"])
    assert blocked_rule["server"] == "proxy-dns"
    assert dns["final"] == "direct-dns"


def test_all_proxy_drops_split_rules() -> None:
    cfg = _config(all_proxy=True)
    tags = {tag for rule in cfg["route"]["rules"] for tag in _rule_set_tags(rule)}
    assert "ru-blocked" not in tags
    assert "ru-inside" not in tags
    assert "ru-direct-manual" in tags
    rs_tags = {rs["tag"] for rs in cfg["route"]["rule_set"]}
    assert rs_tags == {"ru-direct-manual"}
    assert cfg["dns"]["final"] == "proxy-dns"


def test_single_tun_inbound() -> None:
    inbounds = _config()["inbounds"]
    assert len(inbounds) == 1
    assert inbounds[0]["type"] == "tun"
    assert inbounds[0]["auto_route"] is True


def test_render_vless_url_shape() -> None:
    url = render_vless_url(PARAMS, UUID, "телефон Пети")
    assert url.startswith(f"vless://{UUID}@203.0.113.7:8443?")
    assert "security=reality" in url
    assert "flow=xtls-rprx-vision" in url
    assert "pbk=PUBKEYbase64url" in url
    assert "sid=0123456789abcdef" in url
    assert "sni=www.google.com" in url
    assert "type=tcp" in url
    assert url.endswith("#%D1%82%D0%B5%D0%BB%D0%B5%D1%84%D0%BE%D0%BD%20%D0%9F%D0%B5%D1%82%D0%B8")


def test_render_deep_link_wraps_vless_url() -> None:
    url = render_vless_url(PARAMS, UUID, "phone")
    assert render_deep_link(url) == f"hiddify://import/{url}"


@pytest.mark.parametrize("suffix", DIRECT_SUFFIXES)
def test_direct_suffixes_are_clean(suffix: str) -> None:
    assert suffix == suffix.lower()
    assert not suffix.startswith(".")
    assert " " not in suffix
    assert "/" not in suffix
    assert "." in suffix


def test_direct_suffixes_no_duplicates() -> None:
    assert len(DIRECT_SUFFIXES) == len(set(DIRECT_SUFFIXES))
    assert DIRECT_SUFFIXES


PROBE_PARAMS = RealityParams(
    endpoint_host="172.245.159.221",
    port=8443,
    server_public_key="pta_VF3o7pvmIw_X3VDePlqIsgOuyWVGrFBvPxjA-wk",
    short_id="51ccffcdddd273ab",
    sni="www.google.com",
    flow=DEFAULT_FLOW,
)
PROBE_UUID = "c0ffee00-dead-beef-0000-000000000001"


def test_parse_vless_url_round_trips_with_render_vless_url():
    url = render_vless_url(PROBE_PARAMS, PROBE_UUID, "🇳🇱 Rose")
    assert parse_vless_url(url) == (PROBE_PARAMS, PROBE_UUID)


def test_parse_vless_url_defaults_flow_when_absent():
    url = (
        "vless://uuid-1@1.2.3.4:8443?encryption=none&security=reality"
        "&sni=www.google.com&fp=chrome&pbk=PBK&sid=SID&type=tcp#label"
    )
    params, client_uuid = parse_vless_url(url)
    assert client_uuid == "uuid-1"
    assert params.flow == DEFAULT_FLOW


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://example.com", "vless"),
        ("vless://@1.2.3.4:8443?pbk=PBK&sid=SID&sni=x", "uuid"),
        ("vless://uuid-1@1.2.3.4:8443?sni=x&sid=SID", "pbk"),
    ],
)
def test_parse_vless_url_rejects_malformed(url, message):
    with pytest.raises(ValueError, match=message):
        parse_vless_url(url)


def test_render_probe_client_config_socks_inbound_and_reality_outbound():
    config = json.loads(render_probe_client_config(PROBE_PARAMS, PROBE_UUID, socks_port=11080))
    inbound = config["inbounds"][0]
    assert inbound["protocol"] == "socks"
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["port"] == 11080
    assert inbound["settings"]["udp"] is True
    outbound = config["outbounds"][0]
    assert outbound["protocol"] == "vless"
    vnext = outbound["settings"]["vnext"][0]
    user = vnext["users"][0]
    assert user == {"id": PROBE_UUID, "encryption": "none", "flow": DEFAULT_FLOW}
    reality = outbound["streamSettings"]["realitySettings"]
    assert reality["serverName"] == PROBE_PARAMS.sni
    assert reality["publicKey"] == PROBE_PARAMS.server_public_key
    assert reality["shortId"] == PROBE_PARAMS.short_id
    assert vnext["address"] == PROBE_PARAMS.endpoint_host
    assert vnext["port"] == PROBE_PARAMS.port
