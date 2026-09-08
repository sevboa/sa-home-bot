"""reality/client_config.py: валидность sing-box JSON, порядок правил
маршрутизации, форма vless://-ссылки и deep-link. Чистые функции, БД/сети нет."""

from __future__ import annotations

import json

import pytest

from sa_home_bot.reality.client_config import (
    RealityParams,
    render_deep_link,
    render_singbox_config,
    render_vless_url,
)
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
    json.loads(text)  # не бросает


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
    # sniff и hijack-dns идут первыми
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
    blocked_rule = next(r for r in dns["rules"] if r.get("rule_set") == ["ru-blocked"])
    assert blocked_rule["server"] == "proxy-dns"
    assert dns["final"] == "direct-dns"


def test_all_proxy_drops_split_rules() -> None:
    cfg = _config(all_proxy=True)
    tags = {tag for rule in cfg["route"]["rules"] for tag in _rule_set_tags(rule)}
    assert "ru-blocked" not in tags
    assert "ru-inside" not in tags
    assert "ru-direct-manual" in tags  # банки по-прежнему напрямую
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
    # фрагмент — URL-экранированное имя устройства, в самом конце
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
    assert "." in suffix  # это домен, а не голое слово


def test_direct_suffixes_no_duplicates() -> None:
    assert len(DIRECT_SUFFIXES) == len(set(DIRECT_SUFFIXES))
    assert DIRECT_SUFFIXES  # непустой
