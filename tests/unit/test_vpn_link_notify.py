from __future__ import annotations

from sa_home_bot.bot import vpn_link_notify as notify
from sa_home_bot.vpn import protocol as vpn_protocol

LABELS = {"jeeves": "🇳🇱 Нидерланды", "wooster": "🇺🇸 США"}


def label_of(server: str) -> str:
    return LABELS.get(server, "")


LINK_CHANGED = vpn_protocol.EVENT_VPN_LINK_CHANGED
OUTAGE = vpn_protocol.EVENT_VPN_OUTAGE

SCOPES = {
    "global": {"jeeves": "ok", "wooster": "ok"},
    "russia": {"jeeves": "down", "wooster": "partial"},
}
SCOPES_LINE = "🌍 🇳🇱🟢 🇺🇸🟢 · 🇷🇺 🇳🇱🔴 🇺🇸🟡"
LINK_BASE = {"node": "osip", "server": "jeeves", "transport": "awg"}
OUTAGE_BASE = {"scope": "russia", "server": "jeeves"}


def test_server_flag_is_first_word_of_label():
    assert notify.server_flag("🇳🇱 Нидерланды") == "🇳🇱"
    assert notify.server_flag("") == ""


def test_server_title_with_and_without_flag():
    assert notify.server_title("jeeves", "🇳🇱 Нидерланды") == "🇳🇱 <code>jeeves</code>"
    assert notify.server_title("jeeves", "") == "<code>jeeves</code>"
    assert notify.server_title("a<b", "") == "<code>a&lt;b</code>"


def test_scopes_line_in_scope_order():
    assert notify.scopes_line(SCOPES, label_of) == SCOPES_LINE


def test_scopes_line_uses_server_name_without_label():
    assert notify.scopes_line({"global": {"x&y": "down"}}, label_of) == "🌍 x&amp;y🔴"
    assert notify.scopes_line({"global": {"x": "maybe"}}, label_of) == "🌍 x⚪"


def test_scopes_line_empty_for_non_dict():
    assert notify.scopes_line(None, label_of) == ""
    assert notify.scopes_line([], label_of) == ""
    assert notify.scopes_line({"global": {}}, label_of) == ""


def test_link_changed_down_text():
    data = {**LINK_BASE, "to": "down", "scope": "russia", "scopes": SCOPES}
    assert notify.link_changed_text(data, label_of) == (
        "🔴 🇳🇱 <code>jeeves</code> · AmneziaVPN — не работает 🇷🇺 (<code>osip</code>)\n"
        + SCOPES_LINE
    )


def test_link_changed_ok_text_without_scope():
    data = {"node": "osip", "server": "wooster", "transport": "reality", "to": "ok"}
    assert notify.link_changed_text(data, label_of) == (
        "🟢 🇺🇸 <code>wooster</code> · VLESS (Reality) — снова работает (<code>osip</code>)"
    )


def test_link_changed_keeps_unknown_transport_name():
    data = {**LINK_BASE, "transport": "zzz", "to": "down"}
    assert "zzz" in notify.link_changed_text(data, label_of)


def test_link_changed_rejects_partial_and_missing_fields():
    assert notify.link_changed_text({**LINK_BASE, "to": "partial"}, label_of) is None
    no_node = {"server": "jeeves", "transport": "awg", "to": "ok"}
    no_server = {"node": "osip", "transport": "awg", "to": "ok"}
    no_transport = {"node": "osip", "server": "jeeves", "to": "ok"}
    assert notify.link_changed_text(no_node, label_of) is None
    assert notify.link_changed_text(no_server, label_of) is None
    assert notify.link_changed_text(no_transport, label_of) is None


def test_outage_text_down_and_recovery():
    assert notify.outage_text({**OUTAGE_BASE, "to": "down"}, label_of) == (
        "🚨 🇳🇱 <code>jeeves</code> — VPN недоступен 🇷🇺"
    )
    assert notify.outage_text({**OUTAGE_BASE, "from": "down", "to": "ok"}, label_of) == (
        "✅ 🇳🇱 <code>jeeves</code> — VPN снова доступен 🇷🇺"
    )
    assert notify.outage_text({**OUTAGE_BASE, "from": "down", "to": "partial"}, label_of) == (
        "✅ 🇳🇱 <code>jeeves</code> — VPN снова доступен 🇷🇺 (частично)"
    )
    assert notify.outage_text({**OUTAGE_BASE, "from": "ok", "to": "partial"}, label_of) is None
    with_scopes = {**OUTAGE_BASE, "from": "down", "to": "ok", "scopes": SCOPES}
    assert notify.outage_text(with_scopes, label_of) == (
        "✅ 🇳🇱 <code>jeeves</code> — VPN снова доступен 🇷🇺\n" + SCOPES_LINE
    )
    assert notify.outage_text({"server": "jeeves", "scope": "mars", "to": "down"}, label_of) is None
    assert notify.outage_text({"scope": "russia", "to": "down"}, label_of) is None


def test_render_dispatches_by_event_name():
    link = {**LINK_BASE, "to": "down"}
    assert notify.render(LINK_CHANGED, link, label_of) == notify.link_changed_text(link, label_of)
    outage = {"scope": "global", "server": "jeeves", "to": "down"}
    assert notify.render(OUTAGE, outage, label_of) == notify.outage_text(outage, label_of)
    assert notify.render("vpn_check_failed", link, label_of) is None


def test_dedup_key_ignores_scopes_and_depends_on_to():
    first = {**LINK_BASE, "to": "down", "scope": "russia", "scopes": SCOPES}
    second = {**first, "scopes": {"global": {"jeeves": "ok"}}}
    key = notify.dedup_key
    assert key(LINK_CHANGED, first) == key(LINK_CHANGED, second)
    assert key(LINK_CHANGED, {**first, "to": "ok"}) != key(LINK_CHANGED, first)
    assert key(LINK_CHANGED, first) == "vpn_link_changed|osip|jeeves|awg|down"
    outage = {**OUTAGE_BASE, "from": "ok", "to": "down", "scopes": SCOPES}
    assert key(OUTAGE, outage) == "vpn_outage|vpn|russia|jeeves|down"
    node = {"layer": "node", "scope": None, "server": "jeeves", "to": "down"}
    assert key(OUTAGE, node) == "vpn_outage|node||jeeves|down"
    assert key(OUTAGE, {**outage, "to": "ok"}) != key(OUTAGE, outage)


def test_outage_text_names_the_broken_layer():
    net = {**OUTAGE_BASE, "layer": "net", "from": "ok", "to": "down"}
    assert notify.outage_text(net, label_of) == "🚨 🇳🇱 <code>jeeves</code> — сервер не отвечает 🇷🇺"
    back = {**net, "from": "down", "to": "ok"}
    assert (
        notify.outage_text(back, label_of) == "✅ 🇳🇱 <code>jeeves</code> — сервер снова отвечает 🇷🇺"
    )
    node = {"layer": "node", "scope": None, "server": "jeeves", "from": "ok", "to": "down"}
    assert notify.outage_text(node, label_of) == "🚨 🇳🇱 <code>jeeves</code> — нода недоступна"
    assert notify.outage_text({**node, "layer": "vpn"}, label_of) is None
    assert notify.outage_text({**net, "layer": "moon"}, label_of) is None


def test_layers_lines_mute_layers_under_the_broken_one():
    layers = {
        "global": {"jeeves": {"net": "ok", "node": "ok", "vpn": "partial"}},
        "russia": {
            "jeeves": {"net": "down", "node": "ok", "vpn": "ok"},
            "wooster": {"net": None, "node": "down", "vpn": "ok"},
        },
    }
    assert notify.layers_lines(layers, label_of) == [
        "🌍 🇳🇱 🟢🟢🟡",
        "🇷🇺 🇳🇱 🔴⚪⚪ · 🇺🇸 ⚪🔴⚪",
    ]
    assert notify.layers_lines(None, label_of) == []
