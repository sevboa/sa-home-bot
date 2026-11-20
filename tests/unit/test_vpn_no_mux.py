from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from sa_home_bot.reality.client_config import (
    RealityParams,
    render_singbox_config,
    render_singbox_multi,
    render_vless_url,
)
from sa_home_bot.reality.probe_client_config import render_probe_client_config

PARAMS = RealityParams(
    endpoint_host="203.0.113.7",
    port=8443,
    server_public_key="PUBKEYbase64url",
    short_id="0123456789abcdef",
    sni="www.google.com",
)
UUID = "11111111-2222-3333-4444-555555555555"
MULTI = [("nl", PARAMS, UUID), ("se", PARAMS, UUID)]
PROBE_PARAMS = RealityParams(
    endpoint_host="172.245.159.221",
    port=8443,
    server_public_key="pta_VF3o7pvmIw_X3VDePlqIsgOuyWVGrFBvPxjA-wk",
    short_id="51ccffcdddd273ab",
    sni="www.google.com",
)
PROBE_UUID = "c0ffee00-dead-beef-0000-000000000001"


def _walk(value: object) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            assert key not in ("mux", "multiplex")
            _walk(sub)
    elif isinstance(value, list):
        for item in value:
            _walk(item)


def test_client_json_profiles_have_no_mux():
    for cfg in (
        render_singbox_config(PARAMS, UUID),
        render_singbox_config(PARAMS, UUID, all_proxy=True),
        render_singbox_multi(MULTI),
        render_singbox_multi(MULTI, all_proxy=True),
        render_probe_client_config(PROBE_PARAMS, PROBE_UUID, socks_port=11080),
    ):
        _walk(json.loads(cfg))


def test_client_vless_urls_have_no_mux():
    for url in (
        render_vless_url(PARAMS, UUID, "телефон Пети"),
        render_vless_url(PROBE_PARAMS, PROBE_UUID, "🇳🇱 Rose"),
    ):
        query = parse_qs(urlsplit(url).query)
        assert "mux" not in query
        assert "multiplex" not in query
