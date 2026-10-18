from __future__ import annotations

import asyncio

import pytest_asyncio

from sa_home_bot.bot import (
    commands,
    vpn_admin_view,
    vpn_check_view,
    vpn_legacy_card,
    vpn_nodes,
    vpn_proxy_screen,
    vpn_screens,
)
from sa_home_bot.bot.handlers import vpn as vpn_handlers
from sa_home_bot.bot.handlers.vpn import context as hctx
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

from .vpn_support import (
    ADMIN,
    GUEST,
    GUEST_FULL,
    FakeCallback,
    FakeMessage,
    FakeNodeLink,
    FakeNotifier,
    make_config,
    new_pending,
)


async def test_grant_extra_redraws_card(monkeypatch):
    link = FakeNodeLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:grant_extra", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_GRANT_EXTRA in actions
    assert vpn_protocol.ACTION_USAGE in actions


async def test_grant_extra_hits_ceiling_converts_to_request(monkeypatch):
    class ToggleLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            self.calls.append((action, args or {}))
            if action == vpn_protocol.ACTION_GRANT_EXTRA:
                raise ProtoError(vpn_protocol.ERR_QUOTA_CEILING, "потолок")
            return {"request_id": 42, "status": "pending"}

    link = ToggleLink()
    callback = FakeCallback("act:vpn:grant_extra", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert any("№42" in text for text in callback.message.answers)
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_REQUEST_EXTRA in actions


async def test_revoke_calls_service_and_redraws_card():
    link = FakeNodeLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:revoke:Rose", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())
    action, args = link.calls[0]
    assert action == vpn_protocol.ACTION_REVOKE
    assert args == {"chat_id": 777, "device_label": "Rose"}
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_USAGE in actions
    assert any("Rose" in str(a) for a in callback.answered)


async def test_revoke_without_label_is_noop():
    link = FakeNodeLink()
    callback = FakeCallback("act:vpn:revoke", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert link.calls == []
    assert callback.answered


def _server(**over) -> dict:
    return {
        "node": "jeeves",
        "label": "",
        "used_bytes": 0,
        "limit_bytes": 500 * 10**9,
        "remaining_bytes": 500 * 10**9,
        "allowed": True,
        "base_limit_bytes": 500 * 10**9,
        "personal_base": False,
        "devices": [],
        "transports": ["awg"],
        "proxy_available": True,
    } | over


async def test_card_keyboard_offers_revoke_button_per_device():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(devices=[{"device_label": "Rose"}])],
        subscription=GUEST_FULL,
        self_serve_nodes=[],
    )
    device_row = keyboard.inline_keyboard[1]
    texts = [button.text for button in device_row]
    assert any("Отозвать" in text for text in texts)
    assert any("Перевыпустить" in text for text in texts)


async def test_card_keyboard_shows_proxy_on_reality_only_node_with_proxy():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(transports=["reality"], proxy_available=True)],
        subscription=ADMIN,
        self_serve_nodes=[],
    )
    flat = " ".join(b.text for row in keyboard.inline_keyboard for b in row)
    assert "Прокси Telegram" in flat
    assert "Проверка сети" in flat
    assert "Все гости" in flat


def _check(transport: str, status: str, *, server: str = "jeeves", observers: int = 2) -> dict:
    return {"server": server, "transport": transport, "status": status, "observers": observers}


def test_card_shows_transport_health_indicators():
    text = vpn_legacy_card.usage_text(
        [
            _server(
                transports=["awg", "reality"],
                check=[_check("awg", "alerting"), _check("reality", "ok")],
            )
        ]
    )
    assert "🛰 AmneziaVPN 🔴 · VLESS (Reality) 🟢" in text


def test_card_shows_partial_as_orange_with_legend():
    text = vpn_legacy_card.usage_text(
        [_server(transports=["reality"], check=[_check("reality", "partial")])]
    )
    assert "VLESS (Reality) 🟠" in text
    assert "где-то уже блокируют" in text


def test_card_omits_legend_when_everything_is_green():
    text = vpn_legacy_card.usage_text(
        [_server(transports=["reality"], check=[_check("reality", "ok")])]
    )
    assert "🟢" in text
    assert "где-то уже блокируют" not in text


def test_card_without_check_data_shows_no_indicator():
    text = vpn_legacy_card.usage_text([_server(transports=["awg"], check=[])])
    assert "🛰" not in text
    assert "🟢" not in text


def test_server_picker_marks_location_with_worst_case_icon():
    keyboard = vpn_screens.server_picker_keyboard(
        [
            {
                "node": "jeeves",
                "label": "🇳🇱 Нидерланды",
                "transports": ["awg", "reality"],
                "check": [_check("awg", "alerting"), _check("reality", "ok")],
            },
            {"node": "wooster", "label": "🇺🇸 США", "transports": ["reality"], "check": []},
        ]
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "🇳🇱 Нидерланды 🟠" in texts
    assert "🇺🇸 США" in texts


def test_transport_picker_marks_each_technology():
    keyboard = vpn_screens.transport_picker_keyboard(
        ["awg", "reality"],
        "jeeves",
        {"awg": "🔴", "reality": "🟢"},
    )
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert texts[0] == "AmneziaVPN 🔴"
    assert texts[1] == "VLESS (Reality) — для РФ 🟢"


def test_check_status_text_groups_observers_under_pair():
    states = [
        {
            "node": "alfred",
            "server": "wooster",
            "transport": "reality",
            "target": "https://1.1.1.1",
            "status": "alerting",
            "last_latency_ms": None,
            "last_error": "timeout",
        },
        {
            "node": "jeeves",
            "server": "wooster",
            "transport": "reality",
            "target": "https://1.1.1.1",
            "status": "ok",
            "last_latency_ms": 12,
            "last_error": None,
        },
    ]
    text = vpn_check_view.status_text(
        states, rollup=[_check("reality", "partial", server="wooster")]
    )
    assert "<b>wooster</b> · VLESS (Reality) 🟠" in text
    assert "🔴 <code>alfred</code>" in text
    assert "🟢 <code>jeeves</code>" in text
    assert "12 мс" in text


def test_check_status_text_collapses_multiple_targets_per_observer():
    states = [
        {
            "node": "alfred",
            "server": "jeeves",
            "transport": "awg",
            "target": "https://1.1.1.1",
            "status": "ok",
            "last_latency_ms": 631,
            "last_error": None,
        },
        {
            "node": "alfred",
            "server": "jeeves",
            "transport": "awg",
            "target": "https://api.telegram.org",
            "status": "ok",
            "last_latency_ms": 628,
            "last_error": None,
        },
        {
            "node": "alfred",
            "server": "jeeves",
            "transport": "awg",
            "target": "https://www.youtube.com",
            "status": "alerting",
            "last_latency_ms": None,
            "last_error": "curl exit 28",
        },
    ]
    text = vpn_check_view.status_text(states, rollup=[_check("awg", "alerting", server="jeeves")])
    assert text.count("<code>alfred</code>") == 1
    assert "1.1.1.1 631 мс" in text
    assert "telegram 628 мс" in text
    assert "youtube — таймаут" in text
    assert "🔴 <code>alfred</code>" in text


def test_target_label_shortens_known_hosts():
    assert vpn_check_view.target_label("https://1.1.1.1") == "1.1.1.1"
    assert vpn_check_view.target_label("https://api.telegram.org") == "telegram"
    assert vpn_check_view.target_label("https://www.youtube.com") == "youtube"


def test_short_error_maps_curl_timeout():
    assert vpn_check_view.short_error("curl exit 28") == "таймаут"
    assert vpn_check_view.short_error("http 403") == "http 403"
    assert vpn_check_view.short_error(None) == "ошибка"


async def test_card_keyboard_hides_proxy_when_not_configured():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(transports=["reality"], proxy_available=False)],
        subscription=ADMIN,
        self_serve_nodes=[],
    )
    flat = " ".join(b.text for row in keyboard.inline_keyboard for b in row)
    assert "Прокси Telegram" not in flat


class MultiVpnLink(FakeNodeLink):
    state: dict = {
        "node": "jeeves",
        "kind": "vps",
        "peers": [{"id": "wooster", "alive": True, "kind": "vps"}],
        "services": [{"name": "vpn", "service": "vpn", "status": "running"}],
    }


class TwoServersLink(MultiVpnLink):
    vpn_states: dict = {
        "jeeves": {"label": "🇳🇱 Нидерланды", "transports": ["awg", "reality"]},
        "wooster": {"label": "🇺🇸 США", "transports": ["reality"]},
    }
    usages: dict = {
        "jeeves": {
            "node": "jeeves",
            "label": "🇳🇱 Нидерланды",
            "used_bytes": 12 * 10**9,
            "limit_bytes": 500 * 10**9,
            "remaining_bytes": 488 * 10**9,
            "allowed": True,
            "base_limit_bytes": 500 * 10**9,
            "personal_base": False,
            "devices": [{"device_label": "Ромашка", "transport": "awg", "server": "jeeves"}],
            "transports": ["awg", "reality"],
            "proxy_available": True,
        },
        "wooster": {
            "node": "wooster",
            "label": "🇺🇸 США",
            "used_bytes": 3 * 10**9,
            "limit_bytes": 500 * 10**9,
            "remaining_bytes": 497 * 10**9,
            "allowed": True,
            "base_limit_bytes": 500 * 10**9,
            "personal_base": False,
            "devices": [{"device_label": "Лютик", "transport": "reality", "server": "wooster"}],
            "transports": ["reality"],
            "proxy_available": True,
        },
    }
    dead: str | None = None

    async def get_state(self, dst=None):
        if dst is not None and getattr(dst, "service", "") == "vpn":
            if dst.node == self.dead:
                raise ServiceUnavailableError("нода недоступна")
            return self.vpn_states[dst.node]
        return self.state

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        self.dsts.append(dst)
        if dst is not None and dst.node == self.dead:
            raise ServiceUnavailableError("нода недоступна")
        if action == vpn_protocol.ACTION_USAGE and dst is not None:
            return self.usages[dst.node]
        return self._result


async def test_card_keyboard_pins_connection_server_into_callback():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(devices=[{"device_label": "Rose", "server": "wooster"}])],
        subscription=GUEST_FULL,
        self_serve_nodes=[],
    )
    reissue, revoke = keyboard.inline_keyboard[1]
    assert reissue.callback_data == "act:vpn:reissue:Rose:wooster"
    assert revoke.callback_data == "act:vpn:revoke:Rose:wooster"


async def test_card_hides_location_without_access():
    link = TwoServersLink()
    link.usages = {
        "jeeves": link.usages["jeeves"],
        "wooster": link.usages["wooster"] | {"allowed": False},
    }
    error, servers, _down = await hctx.fetch_card(link, 777)
    assert error is None
    assert [s["node"] for s in servers] == ["jeeves"]
    text = vpn_legacy_card.usage_text(servers)
    assert "🇺🇸 США" not in text and "Лютик" not in text


async def test_card_says_access_not_granted_when_nothing_is_open():
    link = TwoServersLink()
    link.usages = {
        node: usage | {"allowed": False} for node, usage in TwoServersLink.usages.items()
    }
    error, servers, _down = await hctx.fetch_card(link, 777)
    assert servers == []
    assert "не выдан" in error
    assert error != hctx.VPN_UNAVAILABLE


async def test_card_without_allowed_field_behaves_as_allowed():
    server = {k: v for k, v in _server().items() if k != "allowed"}
    assert vpn_nodes.allowed_servers([server]) == [server]


async def test_self_serve_button_not_offered_for_closed_location():
    config = make_config()
    config.vpn.warn_remaining_gb = 1000
    closed = _server(allowed=False, remaining_bytes=0)
    assert hctx.self_serve_nodes([closed], config) == []


async def test_server_picker_offers_only_open_locations():
    link = TwoServersLink()
    link.usages = {
        "jeeves": link.usages["jeeves"] | {"allowed": False},
        "wooster": link.usages["wooster"],
    }
    assert [s["node"] for s in await hctx.live_servers_for(link, 777)] == ["wooster"]


async def test_single_open_location_is_pinned_instead_of_first_live():
    link = TwoServersLink()
    link.usages = {
        "jeeves": link.usages["jeeves"] | {"allowed": False},
        "wooster": link.usages["wooster"],
    }
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    issue_dsts = [
        dst
        for action, dst in zip([c[0] for c in link.calls], link.dsts, strict=True)
        if action == vpn_protocol.ACTION_ISSUE
    ]
    assert [dst.node for dst in issue_dsts] == ["wooster"]


async def test_card_merges_devices_from_both_servers():
    error, servers, _down = await hctx.fetch_card(TwoServersLink(), 777)
    assert error is None
    assert [s["node"] for s in servers] == ["jeeves", "wooster"]
    text = vpn_legacy_card.usage_text(servers)
    assert "Ромашка" in text and "Лютик" in text
    assert "🇳🇱 Нидерланды" in text and "🇺🇸 США" in text


async def test_card_keeps_quota_per_server_without_summing():
    _error, servers, _down = await hctx.fetch_card(TwoServersLink(), 777)
    text = vpn_legacy_card.usage_text(servers)
    assert text.count("/ 500 ГБ") == 2
    assert "1000 ГБ" not in text


async def test_card_survives_dead_second_server():
    link = TwoServersLink()
    link.dead = "wooster"
    error, servers, _down = await hctx.fetch_card(link, 777)
    assert error is None
    assert [s["node"] for s in servers] == ["jeeves"]
    assert "Ромашка" in vpn_legacy_card.usage_text(servers)


async def test_grant_extra_button_per_server_near_limit():
    servers = [
        _server(node="jeeves", label="🇳🇱 Нидерланды", remaining_bytes=10 * 10**9),
        _server(node="wooster", label="🇺🇸 США", remaining_bytes=400 * 10**9),
    ]
    keyboard = vpn_legacy_card.card_keyboard(
        servers, subscription=GUEST_FULL, self_serve_nodes=["jeeves"]
    )
    top_row = keyboard.inline_keyboard[0]
    grant = [b for b in top_row if "100 ГБ" in b.text]
    assert len(grant) == 1
    assert "Нидерланды" in grant[0].text
    assert grant[0].callback_data.endswith(":jeeves")


async def test_issue_asks_for_server_when_two_are_alive():
    link = TwoServersLink()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert callback.message.edits and "Где завести" in callback.message.edits[0]
    buttons = [
        b
        for row in callback.message.edit_markups[0].inline_keyboard
        for b in row
        if "Назад" not in b.text
    ]
    assert [b.text for b in buttons] == ["🇳🇱 Нидерланды", "🇺🇸 США"]
    assert [b.callback_data for b in buttons] == [
        "act:vpn:issue::jeeves",
        "act:vpn:issue::wooster",
    ]
    assert vpn_protocol.ACTION_ISSUE not in [c[0] for c in link.calls]


async def test_issue_skips_server_picker_when_single_node():
    link = BothTransportsLink()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert "технолог" in callback.message.edits[0].lower()


async def test_chosen_server_survives_into_transport_picker():
    link = TwoServersLink()
    callback = FakeCallback("act:vpn:issue::jeeves", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert "технолог" in callback.message.edits[0].lower()
    picks = [
        b.callback_data
        for row in callback.message.edit_markups[0].inline_keyboard
        for b in row
        if "Назад" not in b.text
    ]
    assert picks == ["act:vpn:issue:t_awg:jeeves", "act:vpn:issue:t_reality:jeeves"]


async def test_issue_on_chosen_server_goes_to_that_node():
    link = TwoServersLink()
    callback = FakeCallback("act:vpn:issue:t_reality:wooster", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    issue = next(c for c in link.calls if c[0] == vpn_protocol.ACTION_ISSUE)
    assert issue[1]["transport"] == "reality"
    assert link.dsts[link.calls.index(issue)].node == "wooster"


async def test_reissue_button_routes_to_connection_server():
    link = MultiVpnLink(
        result={"config_text": "[Interface]", "device_label": "Rose", "prior_device_count": 1}
    )
    callback = FakeCallback("act:vpn:reissue:Rose:wooster", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(ttl_s=0.01), GUEST, new_pending()
    )
    assert link.calls[0][0] == vpn_protocol.ACTION_REISSUE
    assert link.dsts[0].node == "wooster"
    await asyncio.sleep(0.02)


async def test_revoke_falls_back_to_first_vpn_node_when_server_gone():
    link = MultiVpnLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    callback = FakeCallback("act:vpn:revoke:Rose:archnode", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert link.calls[0][0] == vpn_protocol.ACTION_REVOKE
    assert link.dsts[0].node == "jeeves"


async def test_card_reports_unavailable_when_no_vpn_in_swarm():
    class NoVpn(FakeNodeLink):
        state: dict = {
            "node": "alfred",
            "peers": [],
            "services": [{"name": "monitor", "service": "monitor", "status": "running"}],
        }

    error, servers, _down = await hctx.fetch_card(NoVpn(), 777)
    assert servers == []
    assert "недоступна" in error


async def test_apk_first_click_shows_faq_list_not_file():
    link = FakeNodeLink()
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:apk", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())
    assert callback.message.edits[-1].startswith("❓")
    assert link.calls == []
    assert notifier.sent_documents == []


async def test_apk_send_click_delivers_file_and_hides_button():
    link = FakeNodeLink(result={"telegram_file_id": "cached-id", "version": "2.0.1"})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:apk:send", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())
    assert notifier.sent_documents[0][:2] == (777, "cached-id")
    assert callback.message.reply_markup_cleared


async def test_issue_in_group_chat_is_refused(monkeypatch):
    link = FakeNodeLink()
    callback = FakeCallback("act:vpn:issue", chat_id=-1001234)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert link.calls == []
    assert callback.answered


async def test_issue_first_device_sends_file_first_then_qr_button():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 0,
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())

    assert notifier.sent_documents and notifier.sent_documents[0][0] == 777
    assert b"SECRET" in notifier.sent_documents[0][1]
    assert notifier.sent_photos == []
    assert notifier.sent_direct and notifier.sent_direct[0][0] == 777
    assert notifier.sent_direct_markups[0] is not None


async def test_issue_second_device_sends_qr_first_then_config_button():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 1,
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())

    assert notifier.sent_photos and notifier.sent_photos[0][0] == 777
    assert notifier.sent_documents == []
    assert notifier.sent_direct and notifier.sent_direct[0][0] == 777
    assert notifier.sent_direct_markups[0] is not None


async def test_config_button_delivers_file_and_hides_itself():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "",
            "device_label": "Rose",
            "prior_device_count": 1,
        }
    )
    notifier = FakeNotifier()
    pending = new_pending()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, pending)

    button_markup = notifier.sent_direct_markups[0]
    token_button = button_markup.inline_keyboard[0][0]
    callback2 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback2, link, notifier, make_config(), GUEST, pending)

    assert notifier.sent_documents
    assert b"SECRET" in notifier.sent_documents[0][1]
    assert notifier.sent_documents[0][0] == 777
    assert callback2.message.reply_markup_cleared


async def test_config_button_reveals_qr_when_file_was_first():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 0,
        }
    )
    notifier = FakeNotifier()
    pending = new_pending()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, pending)

    button_markup = notifier.sent_direct_markups[0]
    token_button = button_markup.inline_keyboard[0][0]
    callback2 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback2, link, notifier, make_config(), GUEST, pending)

    assert notifier.sent_photos
    assert notifier.sent_photos[0][0] == 777
    assert callback2.message.reply_markup_cleared


async def test_config_button_used_twice_refuses_second_time():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "",
            "device_label": "Rose",
            "prior_device_count": 1,
        }
    )
    notifier = FakeNotifier()
    pending = new_pending()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, pending)
    token_button = notifier.sent_direct_markups[0].inline_keyboard[0][0]

    callback2 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback2, link, notifier, make_config(), GUEST, pending)
    callback3 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback3, link, notifier, make_config(), GUEST, pending)

    assert len(notifier.sent_documents) == 1
    assert callback3.answered


async def test_issue_secret_cleans_up_after_ttl_without_click():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 1,
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, notifier, make_config(ttl_s=0.01), GUEST, new_pending()
    )

    await asyncio.sleep(0.05)
    assert len(notifier.deleted) == 2


async def test_check_status_edits_card_in_place():
    link = FakeNodeLink(
        result={"states": [{"node": "alfred", "target": "1.1.1.1", "status": "ok"}]}
    )
    callback = FakeCallback("act:vpn:check_status", chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending()
    )
    assert callback.message.answers == []
    assert callback.message.edits and "alfred" in callback.message.edits[0]


def test_check_status_keyboard_has_back_and_refresh_buttons():
    keyboard = vpn_check_view.status_keyboard()
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Назад" in t for t in texts)
    assert any("Обновить" in t for t in texts)
    assert any("Запустить проверку" in t for t in texts)


def test_check_status_text_pending_hint():
    text = vpn_check_view.status_text([], pending=True)
    assert "⏳" in text
    assert "Обновить" in text
    assert "⏳" not in vpn_check_view.status_text([], pending=False)


async def test_check_now_edits_message_with_pending_hint_not_new_message():
    class TwoStepLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            self.calls.append((action, args or {}))
            if action == vpn_protocol.ACTION_CHECK_NOW:
                return {"dispatched_to": ["alfred", "jeeves"]}
            return {"states": [{"node": "alfred", "target": "1.1.1.1", "status": "alerting"}]}

    link = TwoStepLink()
    callback = FakeCallback("act:vpn:check_now", chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending()
    )
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_CHECK_NOW in actions
    assert vpn_protocol.ACTION_CHECK_STATUS in actions
    assert callback.message.answers == []
    assert callback.message.edits
    assert "⏳" in callback.message.edits[0]
    assert "Обновить" in callback.message.edits[0]


async def test_vpn_card_back_button_redraws_card():
    link = FakeNodeLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    callback = FakeCallback("act:vpn:vpn_card", chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending()
    )
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_USAGE in actions
    assert callback.message.edits
    assert callback.message.answers == []


async def test_resolve_request_approve(monkeypatch):
    link = FakeNodeLink(result={"request_id": 7, "status": "approved"})
    callback = FakeCallback("act:vpn:resolve_request:7_approve", chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending()
    )
    action, args = link.calls[0]
    assert action == vpn_protocol.ACTION_RESOLVE_REQUEST
    assert args == {"request_id": 7, "approve": True}
    assert any("одобрена" in text for text in callback.message.answers)


async def test_resolve_request_deny(monkeypatch):
    link = FakeNodeLink(result={"request_id": 7, "status": "denied"})
    callback = FakeCallback("act:vpn:resolve_request:7_deny", chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending()
    )
    action, args = link.calls[0]
    assert args == {"request_id": 7, "approve": False}
    assert any("отклонена" in text for text in callback.message.answers)


class BothTransportsLink(FakeNodeLink):
    def __init__(self, result=None, raises=None, transports=("awg", "reality")) -> None:
        super().__init__(result=result, raises=raises)
        self._transports = list(transports)

    async def get_state(self, dst=None):
        if dst is not None and getattr(dst, "service", None) == "vpn":
            return {"node": "jeeves", "service": "vpn", "transports": self._transports}
        return self.state


async def test_issue_shows_transport_picker_when_node_carries_both():
    link = BothTransportsLink()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    assert callback.message.edits and "технолог" in callback.message.edits[0].lower()
    assert vpn_protocol.ACTION_ISSUE not in [c[0] for c in link.calls]


async def test_single_transport_node_skips_picker():
    link = BothTransportsLink(
        result={
            "config_text": "[Interface]\nPrivateKey = S",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 1,
        },
        transports=("awg",),
    )
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), GUEST, new_pending()
    )
    issue = [c for c in link.calls if c[0] == vpn_protocol.ACTION_ISSUE]
    assert issue and "transport" not in issue[0][1]


async def test_picked_reality_transport_issues_and_sends_singbox_json():
    link = BothTransportsLink(
        result={
            "transport": "reality",
            "config_text": '{"outbounds": []}',
            "share_url": "vless://uuid@1.2.3.4:8443?type=tcp#Rose",
            "deep_link": "hiddify://import/vless://uuid@1.2.3.4:8443?type=tcp#Rose",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 0,
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue:t_reality", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, make_config(), GUEST, new_pending())

    issue = [c for c in link.calls if c[0] == vpn_protocol.ACTION_ISSUE]
    assert issue and issue[0][1].get("transport") == "reality"
    assert notifier.sent_documents and notifier.sent_documents[0][0] == 777
    assert b'"outbounds"' in notifier.sent_documents[0][1]
    assert "VLESS" in (notifier.sent_documents[0][2] or "")
    assert any("hiddify://import" in text for _, text in notifier.sent_direct)


def _book(*guests: Subscription) -> SubscriptionBook:
    return SubscriptionBook(list(guests))


ANYA = Subscription(chat_id=777, name="Аня", source=SOURCE_GUEST)


async def test_all_guests_button_leads_to_admin_screen():
    keyboard = vpn_legacy_card.card_keyboard([_server()], subscription=ADMIN, self_serve_nodes=[])
    flat = [b for row in keyboard.inline_keyboard for b in row]
    button = next(b for b in flat if "Все гости" in b.text)
    assert button.callback_data == vpn_admin_view.guests_cb(0)
    assert commands.parse_action_callback(button.callback_data)[1] == vpn_protocol.ACTION_PEERS


async def test_admin_screen_lists_guests():
    link = TwoServersLink()
    callback = FakeCallback(vpn_admin_view.guests_cb(0), chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending(), _book(ANYA)
    )
    assert "Гости VPN" in callback.message.edits[0]
    assert "Аня" in callback.message.edits[0]


async def test_admin_location_screen_opens_from_guest_card():
    link = TwoServersLink()
    callback = FakeCallback(vpn_admin_view.location_cb(777, "wooster"), chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending(), _book(ANYA)
    )
    assert "🇺🇸" in callback.message.edits[0]
    assert "США" not in callback.message.edits[0]


async def test_set_access_grants_quota_and_tells_the_guest():
    link = TwoServersLink()
    link._result = _server(node="wooster", label="🇺🇸 США", base_limit_bytes=200 * 10**9)
    notifier = FakeNotifier()
    callback = FakeCallback(vpn_admin_view.set_access_cb(777, "wooster", "200"), chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, notifier, make_config(), ADMIN, new_pending(), _book(ANYA)
    )
    call = next(c for c in link.calls if c[0] == vpn_protocol.ACTION_SET_ACCESS)
    assert call[1] == {"chat_id": 777, "base_gb": 200, "allowed": True}
    assert link.dsts[-1].node == "wooster"
    assert notifier.sent_direct and notifier.sent_direct[0][0] == 777
    assert "200 ГБ" in notifier.sent_direct[0][1]


async def test_closing_access_does_not_touch_the_quota():
    link = TwoServersLink()
    link._result = _server(node="wooster", allowed=False)
    callback = FakeCallback(vpn_admin_view.set_access_cb(777, "wooster", "off"), chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending(), _book(ANYA)
    )
    call = next(c for c in link.calls if c[0] == vpn_protocol.ACTION_SET_ACCESS)
    assert call[1] == {"chat_id": 777, "allowed": False}


async def test_set_access_on_stale_node_explains_instead_of_proto_error():
    class StaleNodeLink(TwoServersLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            if action == vpn_protocol.ACTION_SET_ACCESS:
                raise ProtoError(ERR_UNKNOWN_ACTION, "unknown action")
            return await super().command(action, args, dst, timeout=timeout)

    link = StaleNodeLink()
    callback = FakeCallback(vpn_admin_view.set_access_cb(777, "wooster", "on"), chat_id=1)
    await vpn_handlers.handle_action(
        callback, link, FakeNotifier(), make_config(), ADMIN, new_pending(), _book(ANYA)
    )
    said = " ".join(str(a) for a, _ in callback.answered)
    assert "не умеет" in said and "обнов" in said


async def test_admin_screen_without_book_says_so_instead_of_crashing():
    callback = FakeCallback(vpn_admin_view.guests_cb(0), chat_id=1)
    await vpn_handlers.handle_action(
        callback, TwoServersLink(), FakeNotifier(), make_config(), ADMIN, new_pending(), None
    )
    assert callback.answered and not callback.message.edits


@pytest_asyncio.fixture(autouse=True)
async def _drain_pending_tasks():
    yield
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()


PROXY_GUEST = Subscription(
    chat_id=779,
    name="proxy-guest",
    allowed_commands=frozenset({"usage@vpn", "vpn_card@vpn", "proxy_link@vpn"}),
)


async def test_proxy_button_is_gated_by_right_not_by_admin():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(proxy_available=True)], subscription=PROXY_GUEST, self_serve_nodes=[]
    )
    flat = " ".join(b.text for row in keyboard.inline_keyboard for b in row)
    assert "Прокси Telegram" in flat
    assert "Все гости" not in flat


async def test_proxy_button_hidden_without_the_right():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(proxy_available=True)], subscription=GUEST, self_serve_nodes=[]
    )
    flat = " ".join(b.text for row in keyboard.inline_keyboard for b in row)
    assert "Прокси Telegram" not in flat


async def test_card_shows_only_buttons_the_guest_may_press():
    keyboard = vpn_legacy_card.card_keyboard(
        [_server(proxy_available=True, devices=[{"device_label": "Rose"}])],
        subscription=PROXY_GUEST,
        self_serve_nodes=["jeeves"],
    )
    flat = " ".join(b.text for row in keyboard.inline_keyboard for b in row)
    assert "Новое устройство" not in flat
    assert "Перевыпустить" not in flat
    assert "Отозвать" not in flat
    assert "Приложение" not in flat
    assert "100 ГБ" not in flat
    assert all(row for row in keyboard.inline_keyboard)


async def test_vpn_card_falls_back_to_proxy_when_no_location_is_open():
    link = TwoServersLink()
    link.usages = {
        node: usage | {"allowed": False} for node, usage in TwoServersLink.usages.items()
    }
    message = FakeMessage(779)
    await vpn_handlers.cmd_vpn(message, link, make_config(), PROXY_GUEST)
    assert "Прокси Telegram вам открыт" in message.answers[0]
    flat = [b.text for row in message.answer_markups[0].inline_keyboard for b in row]
    assert flat == ["✈️ Прокси Telegram"]


async def test_vpn_card_still_refuses_guest_without_proxy():
    link = TwoServersLink()
    link.usages = {
        node: usage | {"allowed": False} for node, usage in TwoServersLink.usages.items()
    }
    message = FakeMessage(777)
    await vpn_handlers.cmd_vpn(message, link, make_config(), GUEST)
    assert "не выдан" in message.answers[0]


def _proxy_result() -> dict:
    return {
        "tg_link": "tg://proxy?server=1.2.3.4&port=443&secret=deadbeef",
        "t_me_link": "https://t.me/proxy?server=1.2.3.4&port=443&secret=deadbeef",
        "host": "1.2.3.4",
        "port": 443,
        "secret": "deadbeef",
        "socks_host": "100.109.139.95",
        "socks_port": 1080,
        "node": "jeeves",
        "label": "🇳🇱 Нидерланды",
    }


def test_proxy_text_keeps_socks_address_for_admin_only():
    result = _proxy_result()
    assert "100.109.139.95" in vpn_proxy_screen.proxy_text(result, admin=True)
    guest_text = vpn_proxy_screen.proxy_text(result, admin=False)
    assert "100.109.139.95" not in guest_text
    assert "SOCKS5" not in guest_text
    assert "tg://proxy" in guest_text and "deadbeef" in guest_text


def test_proxy_keyboard_has_no_rotate_button_for_guest():
    assert vpn_proxy_screen.rotate_keyboard("jeeves", can_rotate=False) is None
    admin_kb = vpn_proxy_screen.rotate_keyboard("jeeves", can_rotate=True)
    assert admin_kb is not None
    assert "Сменить секрет" in admin_kb.inline_keyboard[0][0].text


class FakeGate:
    def __init__(self, book: SubscriptionBook, guest_only: bool = True) -> None:
        self._book = book
        self._guest_only = guest_only
        self.saved: list[tuple[int, frozenset[str]]] = []

    def set_guest_rights(self, chat_id: int, rights: frozenset[str]):
        sub = self._book.for_chat(chat_id)
        if sub is None or (self._guest_only and not sub.is_guest):
            return None
        self.saved.append((chat_id, rights))
        updated = sub.with_allowed_commands(rights)
        self._book.add(updated)
        return updated


async def test_rights_screen_opens_from_guest_card():
    book = _book(ANYA)
    callback = FakeCallback(vpn_admin_view.rights_cb(777), chat_id=1)
    await vpn_handlers.handle_action(
        callback, TwoServersLink(), FakeNotifier(), make_config(), ADMIN, new_pending(), book
    )
    assert "Права VPN" in callback.message.edits[0]


async def test_toggle_grants_the_whole_skill_at_once():
    book = _book(ANYA)
    gate = FakeGate(book)
    callback = FakeCallback(vpn_admin_view.toggle_cb(777, "proxy"), chat_id=1)
    await vpn_handlers.handle_action(
        callback,
        TwoServersLink(),
        FakeNotifier(),
        make_config(),
        ADMIN,
        new_pending(),
        book,
        gate,
        None,
    )
    assert gate.saved == [(777, frozenset({"proxy_link@vpn"}))]
    assert "✅" in callback.message.edits[0]


async def test_toggle_is_idempotent_pair():
    book = _book(ANYA)
    gate = FakeGate(book)
    for _ in range(2):
        callback = FakeCallback(vpn_admin_view.toggle_cb(777, "dev"), chat_id=1)
        await vpn_handlers.handle_action(
            callback,
            TwoServersLink(),
            FakeNotifier(),
            make_config(),
            ADMIN,
            new_pending(),
            book,
            gate,
            None,
        )
    assert gate.saved[-1] == frozenset() or gate.saved[-1][1] == frozenset()
    assert book.for_chat(777).allowed_commands == frozenset()


async def test_toggle_refuses_subscription_outside_the_guest_list():
    owner = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
    book = _book(owner)
    gate = FakeGate(book)
    callback = FakeCallback(vpn_admin_view.toggle_cb(1, "proxy"), chat_id=1)
    await vpn_handlers.handle_action(
        callback,
        TwoServersLink(),
        FakeNotifier(),
        make_config(),
        ADMIN,
        new_pending(),
        book,
        gate,
        None,
    )
    assert gate.saved == []
    assert "больше не в списке" in str(callback.answered[-1])


def test_usage_text_marks_unavailable_server():
    text = vpn_legacy_card.usage_text(
        [_server()],
        unavailable=[{"node": "wooster", "label": "🇺🇸 США", "unavailable": True}],
    )
    assert "🇺🇸 США</b>: 🔌 Сервер недоступен" in text


async def test_card_shows_unavailable_server_to_admin_only(monkeypatch):
    vpn_nodes.remember_servers([{"node": "wooster", "label": "🇺🇸 США"}])

    async def only_jeeves(*_args, **_kwargs):
        return [_server(node="jeeves")]

    monkeypatch.setattr(vpn_nodes, "fanout", only_jeeves)
    admin = Subscription(name="o", chat_id=1, allowed_commands=frozenset({"*"}))
    guest = Subscription(name="g", chat_id=2, source="guest")
    _error, _servers, down = await hctx.fetch_card(object(), 777, admin)
    assert [d["node"] for d in down] == ["wooster"]
    _error, _servers, down = await hctx.fetch_card(object(), 777, guest)
    assert down == []


def test_card_marks_broken_device_with_wrench_and_reissue_hint():
    server = _server(devices=[{"device_label": "Rose", "server": "jeeves", "broken": True}])
    text = vpn_legacy_card.usage_text([server])
    assert "🔧 «Rose» не отвечает" in text and "перевыпустите" in text


def test_card_does_not_mark_healthy_or_old_node_device():
    healthy = {"device_label": "Rose", "server": "jeeves", "broken": False}
    old_node = {"device_label": "Lily", "server": "jeeves"}
    text = vpn_legacy_card.usage_text([_server(devices=[healthy, old_node])])
    assert "🔧" not in text


def test_card_unavailable_server_shows_no_wrench():
    text = vpn_legacy_card.usage_text(
        [_server(devices=[{"device_label": "Rose", "server": "jeeves", "broken": False}])],
        unavailable=[{"node": "wooster", "label": "🇺🇸 США"}],
    )
    assert "🔌 Сервер недоступен" in text and "🔧" not in text
