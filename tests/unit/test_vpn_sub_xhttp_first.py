from __future__ import annotations

import json

from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import subscription as subs


def _xhttp(name: str = "🇳🇱 XHTTP 2087") -> subs.SubEntry:
    return subs.SubEntry(
        node="jeeves",
        name=name,
        uuid="uuid-x",
        params=RealityParams(
            "198.51.100.1",
            2087,
            "PUB1",
            "ab",
            "www.google.com",
            flow="",
            network="xhttp",
            xhttp_path="/ring",
            xhttp_mode="auto",
        ),
        is_xhttp=True,
    )


def _reality(name: str = "🇳🇱 Нидерланды", is_alt: bool = False) -> subs.SubEntry:
    return subs.SubEntry(
        node="jeeves",
        name=name,
        uuid="uuid-nl",
        params=RealityParams("198.51.100.1", 8443, "PUB1", "ab", "www.google.com"),
        is_alt=is_alt,
    )


def _sub(entries: list[subs.SubEntry]) -> subs.Subscription:
    return subs.Subscription(
        "📱 iPhone", tuple(subs.sort_entries(entries)), 5_000, 200_000, 1_800_000_000
    )


def test_sort_entries_xhttp_first_then_reality_then_alt():
    entries = [
        _reality("🇺🇸 США"),
        _reality("🇳🇱 Нидерланды · 2", is_alt=True),
        _reality(),
        _xhttp(),
    ]
    order = subs.sort_entries(entries)
    assert [e.is_xhttp for e in order] == [True, False, False, False]
    assert [e.is_alt for e in order] == [False, False, False, True]
    assert [e.name for e in order] == [
        "🇳🇱 XHTTP 2087",
        "🇳🇱 Нидерланды",
        "🇺🇸 США",
        "🇳🇱 Нидерланды · 2",
    ]


def test_sort_entries_keeps_name_node_order_within_groups():
    entries = [
        _reality("🇺🇸 США"),
        _reality("🇳🇱 Нидерланды · 2", is_alt=True),
        _reality("🇺🇸 США · 2"),
        _reality(),
        _xhttp("🇳🇱 XHTTP 2087 · 2"),
    ]
    order = subs.sort_entries(entries)
    assert [e.name for e in order] == [
        "🇳🇱 XHTTP 2087 · 2",
        "🇳🇱 Нидерланды",
        "🇺🇸 США",
        "🇺🇸 США · 2",
        "🇳🇱 Нидерланды · 2",
    ]


def test_singbox_defaults_to_xhttp_and_auto_starts_with_it():
    body, mime = subs.render_body(
        _sub([_reality(), _reality("🇺🇸 США"), _xhttp()]), subs.FORMAT_SINGBOX
    )
    assert mime == "application/json; charset=utf-8"
    outs = {o["tag"]: o for o in json.loads(body)["outbounds"]}
    assert outs["proxy"]["default"] == "🇳🇱 XHTTP 2087"
    assert outs["proxy"]["outbounds"][:2] == ["auto", "🇳🇱 XHTTP 2087"]
    assert outs["auto"]["outbounds"][0] == "🇳🇱 XHTTP 2087"


def test_singbox_without_xhttp_keeps_auto_default():
    body, _ = subs.render_body(_sub([_reality(), _reality("🇺🇸 США")]), subs.FORMAT_SINGBOX)
    outs = {o["tag"]: o for o in json.loads(body)["outbounds"]}
    assert outs["proxy"]["default"] == "auto"
    assert outs["proxy"]["outbounds"] == ["auto", "🇳🇱 Нидерланды", "🇺🇸 США"]
    assert outs["auto"]["outbounds"][0] == "🇳🇱 Нидерланды"
