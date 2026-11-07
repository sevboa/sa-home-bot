from __future__ import annotations

from sa_home_bot.bot.node_events import render_vpn_check
from sa_home_bot.vpn import protocol as vpn_protocol

FAILED = vpn_protocol.EVENT_VPN_CHECK_FAILED
RECOVERED = vpn_protocol.EVENT_VPN_CHECK_RECOVERED
BASE = {"node": "jeeves", "server": "wooster", "transport": "awg"}


def test_all_failed_one_message_with_all():
    targets = [{"target": "https://1.1.1.1", "error": "timeout"}] * 2
    data = {**BASE, "targets": targets, "total": 2, "all_failed": True, "consecutive": 2}
    text = render_vpn_check(FAILED, data)
    assert "недоступны все сайты" in text
    assert "<code>wooster</code> awg" in text
    assert "<code>1.1.1.1</code>" in text and "— timeout" in text
    assert "https://" not in text


def test_partial_lists_sites_with_own_errors():
    targets = [
        {"target": "https://api.telegram.org", "error": "timeout"},
        {"target": "https://www.google.com", "error": "http 503"},
    ]
    data = {**BASE, "targets": targets, "total": 6, "all_failed": False}
    text = render_vpn_check(FAILED, data)
    assert "все сайты" not in text
    assert "<code>api.telegram.org</code> — timeout" in text
    assert "<code>www.google.com</code> — http 503" in text


def test_recovered_and_legacy_single_target():
    data = {**BASE, "targets": ["https://a.example"], "total": 1, "all_ok": True}
    assert render_vpn_check(RECOVERED, data).startswith("✅")
    legacy = {**BASE, "target": "https://1.1.1.1", "error": "x"}
    assert "<code>1.1.1.1</code>" in render_vpn_check(FAILED, legacy)
    assert render_vpn_check(FAILED, {"node": "jeeves"}) is None


def test_series_failed_says_what_and_where():
    from sa_home_bot.bot.node_events import render_vpn_series

    text = render_vpn_series(
        vpn_protocol.EVENT_VPN_SERIES_FAILED,
        {
            "node": "zakhar",
            "server": "jeeves",
            "transport": "reality",
            "verdict": "freeze",
            "at_bytes": 16384,
            "consecutive": 2,
        },
    )
    assert text == (
        "⚠️ Из РФ (<code>zakhar</code>) не работает VLESS (Reality) → <code>jeeves</code>: "
        "заморозка после 16 КБ (2 замера подряд)"
    )


def test_series_recovered_and_incomplete():
    from sa_home_bot.bot.node_events import render_vpn_series

    data = {"node": "zakhar", "server": "jeeves", "transport": "awg"}
    assert render_vpn_series(vpn_protocol.EVENT_VPN_SERIES_RECOVERED, data) == (
        "✅ Из РФ (<code>zakhar</code>) снова работает AmneziaVPN → <code>jeeves</code>"
    )
    assert render_vpn_series(vpn_protocol.EVENT_VPN_SERIES_FAILED, {"node": "zakhar"}) is None


def test_recovered_active_removes_https_and_www_prefix():
    active = [
        {
            "node": "alfred",
            "server": "wooster",
            "transport": "awg",
            "targets": ["https://www.instagram.com"],
            "whole": False,
        }
    ]
    data = {**BASE, "targets": ["https://b.example"], "total": 1, "all_ok": True, "active": active}
    text = render_vpn_check(RECOVERED, data)
    assert "instagram.com" in text
    assert "https://" not in text
    assert "www." not in text


def test_recovered_active_whole_true_shows_all_sites():
    active = [
        {
            "node": "alfred",
            "server": "wooster",
            "transport": "awg",
            "targets": ["https://a.example"],
            "whole": True,
        }
    ]
    data = {**BASE, "targets": ["https://b.example"], "total": 1, "all_ok": True, "active": active}
    text = render_vpn_check(RECOVERED, data)
    assert "все сайты" in text


def test_recovered_active_two_groups_two_lines():
    active = [
        {
            "node": "alfred",
            "server": "wooster",
            "transport": "awg",
            "targets": ["https://a.example"],
            "whole": False,
        },
        {
            "node": "jeeves",
            "server": "jeeves",
            "transport": "reality",
            "targets": ["https://b.example"],
            "whole": False,
        },
    ]
    data = {**BASE, "targets": ["https://c.example"], "total": 1, "all_ok": True, "active": active}
    text = render_vpn_check(RECOVERED, data)
    assert "alfred → wooster awg: a.example" in text
    assert "jeeves → jeeves reality: b.example" in text


def test_recovered_active_empty_shows_no_other_problems():
    data = {**BASE, "targets": ["https://a.example"], "total": 1, "all_ok": True, "active": []}
    text = render_vpn_check(RECOVERED, data)
    assert "Других проблем нет." in text


def test_recovered_without_active_key_unchanged():
    data = {**BASE, "targets": ["https://a.example"], "total": 1, "all_ok": True}
    text_without = render_vpn_check(RECOVERED, data)
    data_with_empty = {**data, "active": []}
    text_with_empty = render_vpn_check(RECOVERED, data_with_empty)
    assert text_without != text_with_empty
    assert "Других проблем нет." not in text_without


def test_failed_no_active_block_even_if_passed():
    active = [
        {
            "node": "alfred",
            "server": "wooster",
            "transport": "awg",
            "targets": ["https://a.example"],
            "whole": True,
        }
    ]
    data = {
        **BASE,
        "targets": [{"target": "https://b.example", "error": "timeout"}],
        "total": 1,
        "all_failed": True,
        "active": active,
    }
    text = render_vpn_check(FAILED, data)
    assert "Других проблем нет." not in text
    assert "⚠️ Ещё не работает:" not in text
