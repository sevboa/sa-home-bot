from __future__ import annotations

import pytest

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


RECOVERED_DATA = {**BASE, "targets": ["https://a.example"], "total": 6, "all_ok": False}
RECOVERED_TEXT = (
    "✅ VPN: пробник <code>jeeves</code> → <code>wooster</code> awg — снова доступны сайты:\n"
    "<code>a.example</code>"
)


def _problem(node: str, targets: list[str], *, whole: bool = False) -> dict:
    return {
        "node": node,
        "server": "jeeves",
        "transport": "reality",
        "targets": targets,
        "whole": whole,
    }


@pytest.mark.parametrize(
    ("active", "block"),
    [
        ([], "\n\nДругих проблем нет."),
        (
            [_problem("zakhar", ["https://www.instagram.com", "https://api.telegram.org"])],
            "\n\n⚠️ Ещё не работает:\nzakhar → jeeves reality: instagram.com, api.telegram.org",
        ),
        (
            [_problem("zakhar", ["https://1.1.1.1"], whole=True)],
            "\n\n⚠️ Ещё не работает:\nzakhar → jeeves reality: все сайты",
        ),
        (
            [_problem("alfred", ["https://a.example"]), _problem("zakhar", ["https://b.example"])],
            "\n\n⚠️ Ещё не работает:\n"
            "alfred → jeeves reality: a.example\n"
            "zakhar → jeeves reality: b.example",
        ),
        (
            [_problem("a<b", ["https://x&y.example"])],
            "\n\n⚠️ Ещё не работает:\na&lt;b → jeeves reality: x&amp;y.example",
        ),
    ],
)
def test_recovered_shows_active_problems(active, block):
    text = render_vpn_check(RECOVERED, {**RECOVERED_DATA, "active": active})
    assert text == RECOVERED_TEXT + block


def test_recovered_without_active_key_keeps_old_text():
    assert render_vpn_check(RECOVERED, RECOVERED_DATA) == RECOVERED_TEXT


def test_failed_never_shows_active_block():
    targets = [{"target": "https://b.example", "error": "timeout"}]
    data = {**BASE, "targets": targets, "total": 6, "all_failed": False}
    active = [_problem("zakhar", ["https://a.example"])]
    assert render_vpn_check(FAILED, {**data, "active": active}) == render_vpn_check(FAILED, data)
