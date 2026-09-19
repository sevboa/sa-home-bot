"""reality/probe_client_config.py: конфиг xray-клиента для VPN-пробника
(39.0.7(e)) — SOCKS5-инбаунд + VLESS/Reality outbound, и разбор
``vless://``-ссылки обратно в параметры."""

from __future__ import annotations

import json

from sa_home_bot.reality.client_config import RealityParams, render_vless_url
from sa_home_bot.reality.probe_client_config import (
    parse_vless_url,
    render_probe_client_config,
)

_PARAMS = RealityParams(
    endpoint_host="172.245.159.221",
    port=8443,
    server_public_key="pta_VF3o7pvmIw_X3VDePlqIsgOuyWVGrFBvPxjA-wk",
    short_id="51ccffcdddd273ab",
    sni="www.google.com",
    flow="xtls-rprx-vision",
)
_UUID = "c0ffee00-dead-beef-0000-000000000001"


def test_parse_vless_url_round_trips_with_render_vless_url():
    url = render_vless_url(_PARAMS, _UUID, "🇳🇱 Rose")
    params, client_uuid = parse_vless_url(url)
    assert params == _PARAMS
    assert client_uuid == _UUID


def test_parse_vless_url_defaults_flow_when_absent():
    url = (
        "vless://uuid-1@1.2.3.4:8443?encryption=none&security=reality"
        "&sni=www.google.com&fp=chrome&pbk=PBK&sid=SID&type=tcp#label"
    )
    params, client_uuid = parse_vless_url(url)
    assert client_uuid == "uuid-1"
    assert params.flow == "xtls-rprx-vision"


def test_parse_vless_url_rejects_wrong_scheme():
    try:
        parse_vless_url("https://example.com")
    except ValueError as exc:
        assert "vless" in str(exc)
    else:
        raise AssertionError("ожидался ValueError")


def test_parse_vless_url_rejects_missing_uuid():
    try:
        parse_vless_url("vless://@1.2.3.4:8443?pbk=PBK&sid=SID&sni=x")
    except ValueError as exc:
        assert "uuid" in str(exc)
    else:
        raise AssertionError("ожидался ValueError")


def test_parse_vless_url_rejects_missing_required_param():
    try:
        parse_vless_url("vless://uuid-1@1.2.3.4:8443?sni=x&sid=SID")  # без pbk
    except ValueError as exc:
        assert "pbk" in str(exc)
    else:
        raise AssertionError("ожидался ValueError")


def test_render_probe_client_config_socks_inbound():
    content = render_probe_client_config(_PARAMS, _UUID, socks_port=11080)
    config = json.loads(content)
    inbound = config["inbounds"][0]
    assert inbound["protocol"] == "socks"
    assert inbound["listen"] == "127.0.0.1"
    assert inbound["port"] == 11080
    assert inbound["settings"]["udp"] is True


def test_render_probe_client_config_reality_outbound():
    content = render_probe_client_config(_PARAMS, _UUID, socks_port=11080)
    config = json.loads(content)
    outbound = config["outbounds"][0]
    assert outbound["protocol"] == "vless"
    user = outbound["settings"]["vnext"][0]["users"][0]
    assert user["id"] == _UUID
    assert user["flow"] == "xtls-rprx-vision"
    assert user["encryption"] == "none"
    reality = outbound["streamSettings"]["realitySettings"]
    assert reality["serverName"] == _PARAMS.sni
    assert reality["publicKey"] == _PARAMS.server_public_key
    assert reality["shortId"] == _PARAMS.short_id
    assert outbound["settings"]["vnext"][0]["address"] == _PARAMS.endpoint_host
    assert outbound["settings"]["vnext"][0]["port"] == _PARAMS.port
