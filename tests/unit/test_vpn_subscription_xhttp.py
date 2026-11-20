from __future__ import annotations

from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import subscription as subs


def _xhttp_params() -> RealityParams:
    return RealityParams(
        "198.51.100.1",
        2087,
        "PUB1",
        "ab",
        "www.google.com",
        flow="",
        network="xhttp",
        xhttp_path="/ring",
        xhttp_mode="auto",
    )


def _params(port: int = 8443, sni: str = "www.google.com") -> RealityParams:
    return RealityParams("198.51.100.1", port, "PUB1", "ab", sni)


def _xhttp_entry(name: str = "🇳🇱 Нидерланды · XHTTP") -> subs.SubEntry:
    return subs.SubEntry(
        node="jeeves",
        name=name,
        uuid="uuid-nl",
        params=_xhttp_params(),
        is_xhttp=True,
    )


def _entry(name: str = "🇳🇱 Нидерланды", is_alt: bool = False) -> subs.SubEntry:
    return subs.SubEntry(
        node="jeeves",
        name=name,
        uuid="uuid-nl",
        params=_params(),
        is_alt=is_alt,
    )


def _sub(entries: list[subs.SubEntry]) -> subs.Subscription:
    return subs.Subscription(
        "📱 iPhone",
        tuple(subs.sort_entries(entries)),
        5_000,
        200_000,
        1_800_000_000,
    )


def test_subentry_xhttp_roundtrip_preserves_network_path_mode():
    e = _xhttp_entry()
    wire = e.to_wire()
    parsed = subs.SubEntry.from_wire(wire)
    assert parsed.is_xhttp is True
    assert parsed.params.network == "xhttp"
    assert parsed.params.xhttp_path == "/ring"
    assert parsed.params.xhttp_mode == "auto"
    assert parsed.params.flow == ""


def test_subentry_tcp_wire_has_no_network_key():
    wire = _entry().to_wire()
    assert "network" not in wire["params"]


def test_sort_entries_puts_xhttp_last():
    entries = [_entry(), _xhttp_entry(), _entry("🇳🇱 Нидерланды · 2", is_alt=True)]
    assert [e.is_xhttp for e in subs.sort_entries(entries)] == [False, False, True]
    assert [e.name for e in subs.sort_entries(entries)] == [
        "🇳🇱 Нидерланды",
        "🇳🇱 Нидерланды · 2",
        "🇳🇱 Нидерланды · XHTTP",
    ]


def test_expand_spares_does_not_multiply_xhttp():
    entries = [_entry(), _entry("🇳🇱 Нидерланды · 2", is_alt=True), _xhttp_entry()]
    result = subs.expand_spares(entries, ["firefox"])
    assert len(result) == 4
    assert [e.name for e in result] == [
        "🇳🇱 Нидерланды",
        "🇳🇱 Нидерланды · 2",
        "🇳🇱 Нидерланды · XHTTP",
        "🇳🇱 Нидерланды · 2 · firefox",
    ]


def test_render_body_singbox_skips_xhttp():
    sub = _sub([_entry(), _xhttp_entry()])
    body, mime = subs.render_body(sub, subs.FORMAT_SINGBOX)
    assert mime == "application/json; charset=utf-8"
    assert "type=xhttp" not in body
    assert "2087" not in body


def test_render_body_plain_xhttp_last():
    sub = _sub([_entry(), _xhttp_entry()])
    body, mime = subs.render_body(sub, subs.FORMAT_PLAIN)
    assert mime == "text/plain; charset=utf-8"
    lines = body.splitlines()
    assert len(lines) == 2
    assert "type=xhttp" in lines[1]
    assert "path=%2Fring" in lines[1] or "path=/ring" in lines[1]


def test_countries_card_skips_xhttp_entries():
    sub = _sub([_entry(), _xhttp_entry()])
    card = subs._countries_card(sub)
    assert "Нидерланды" in card
    assert "· XHTTP" not in card
