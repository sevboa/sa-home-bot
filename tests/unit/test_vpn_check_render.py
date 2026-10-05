"""Текст алерта vpn_check: одно сообщение на прогон списка сайтов через
туннель, «все» при полном отказе, адреса в <code> (иначе Telegram лепит
превью ссылок)."""

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
