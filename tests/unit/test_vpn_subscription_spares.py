from __future__ import annotations

import json

import pytest

from sa_home_bot.config import RealityTransportConfig
from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import subscription as subs


def test_reality_transport_config_spare_fingerprints_default_empty():
    cfg = RealityTransportConfig()
    assert cfg.spare_fingerprints == []


def test_reality_transport_config_spare_fingerprints_accepts_valid():
    cfg = RealityTransportConfig(spare_fingerprints=["firefox", "edge"])
    assert cfg.spare_fingerprints == ["firefox", "edge"]


def test_reality_transport_config_spare_fingerprints_rejects_invalid():
    with pytest.raises(ValueError, match="fingerprint 'opera' не в допустимом списке"):
        RealityTransportConfig(spare_fingerprints=["opera"])


def test_reality_transport_config_spare_fingerprints_rejects_duplicates():
    with pytest.raises(ValueError, match="дубликат fingerprint: 'firefox'"):
        RealityTransportConfig(spare_fingerprints=["firefox", "firefox"])


def test_expand_spares_empty_list_returns_same_entries():
    entries = [
        subs.SubEntry(
            "node1",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams("198.51.100.1", 8443, "PUB1", "ab", "www.google.com"),
        ),
        subs.SubEntry(
            "node2",
            "🇺🇸 США",
            "uuid-us",
            RealityParams("198.51.100.2", 8443, "PUB2", "ab", "www.google.com"),
        ),
    ]
    result = subs.expand_spares(entries, [])
    assert result == entries


def test_expand_spares_adds_spare_entries():
    entries = [
        subs.SubEntry(
            "node1",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams(
                "198.51.100.1", 8443, "PUB1", "ab", "www.google.com", fingerprint="chrome"
            ),
        ),
        subs.SubEntry(
            "node2",
            "🇺🇸 США",
            "uuid-us",
            RealityParams(
                "198.51.100.2", 8443, "PUB2", "ab", "www.google.com", fingerprint="chrome"
            ),
        ),
    ]
    result = subs.expand_spares(entries, ["firefox"])
    assert len(result) == 4
    assert result[0].name == "🇳🇱 Нидерланды"
    assert result[1].name == "🇺🇸 США"
    assert result[2].name == "🇳🇱 Нидерланды · firefox"
    assert result[3].name == "🇺🇸 США · firefox"
    assert result[2].params.fingerprint == "firefox"
    assert result[3].params.fingerprint == "firefox"


def test_expand_spares_skips_same_fingerprint():
    entries = [
        subs.SubEntry(
            "node1",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams(
                "198.51.100.1", 8443, "PUB1", "ab", "www.google.com", fingerprint="firefox"
            ),
        ),
    ]
    result = subs.expand_spares(entries, ["firefox", "edge"])
    assert len(result) == 2
    assert result[0].name == "🇳🇱 Нидерланды"
    assert result[0].params.fingerprint == "firefox"
    assert result[1].name == "🇳🇱 Нидерланды · edge"
    assert result[1].params.fingerprint == "edge"


def test_render_plain_includes_spare_with_fp_and_encoded_name():
    entries = [
        subs.SubEntry(
            "node1",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams(
                "198.51.100.1", 8443, "PUB1", "ab", "www.google.com", fingerprint="chrome"
            ),
        ),
    ]
    expanded = subs.expand_spares(entries, ["firefox"])
    sub = subs.Subscription("📱 iPhone", tuple(expanded), 0, 0, 0)
    body, ctype = subs.render_body(sub, subs.FORMAT_PLAIN)
    lines = body.splitlines()
    assert len(lines) == 2
    assert "fp=chrome" in lines[0]
    assert "fp=firefox" in lines[1]
    assert "%C2%B7%20firefox" in lines[1] or "·%20firefox" in lines[1]


def test_render_singbox_spare_outbound_tag_contains_fingerprint():
    entries = [
        subs.SubEntry(
            "node1",
            "🇳🇱 Нидерланды",
            "uuid-nl",
            RealityParams(
                "198.51.100.1", 8443, "PUB1", "ab", "www.google.com", fingerprint="chrome"
            ),
        ),
    ]
    expanded = subs.expand_spares(entries, ["firefox"])
    sub = subs.Subscription("📱 iPhone", tuple(expanded), 0, 0, 0)
    body, ctype = subs.render_body(sub, subs.FORMAT_SINGBOX)
    cfg = json.loads(body)
    tags = {o["tag"]: o for o in cfg["outbounds"]}
    assert "🇳🇱 Нидерланды" in tags
    assert "🇳🇱 Нидерланды · firefox" in tags
    assert tags["🇳🇱 Нидерланды · firefox"]["tls"]["utls"]["fingerprint"] == "firefox"
