"""bot/handlers/vpn.py: экспертный режим (57.3) — главная, список, карточка
устройства, удаление с подтверждением, «🔧 Починить страну»."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import commands, vpn_nodes
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot.handlers import vpn as h
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

from .test_vpn_handler import (
    ADMIN,
    GUEST_FULL,
    FakeCallback,
    FakeNodeLink,
    FakeNotifier,
    _config,
)

GB = 10**9


def _conn(transport, hs=None, *, broken=False, created="2026-10-01T10:00:00+00:00"):
    return {
        "transport": transport,
        "status": "active",
        "last_handshake_at": hs,
        "created_at": created,
        "broken": broken,
        "used_bytes": 0,
    }


def _srv(node, label, entries, **over):
    return {
        "node": node,
        "label": label,
        "allowed": True,
        "limit_bytes": 100 * GB,
        "remaining_bytes": 87 * GB,
        "used_bytes": 13 * GB,
        "transports": ["reality", "awg"],
        "proxy_available": True,
        "device_usage": entries,
        "devices": [],
        "check": [],
    } | over


def _servers(*, broken=True):
    nl = _srv(
        "jeeves",
        "🇳🇱 Нидерланды",
        [
            {
                "device_label": "📱 Очень длинное имя устройства для проверки лимита",
                "used_bytes": int(8.7 * GB),
                "connections": [_conn("reality", "2026-10-10T14:02:00+00:00")],
            }
        ],
    )
    us = _srv(
        "wooster",
        "🇺🇸 США",
        [
            {
                "device_label": "📱 Очень длинное имя устройства для проверки лимита",
                "used_bytes": int(0.4 * GB),
                "connections": [_conn("reality"), _conn("awg", broken=broken)],
            }
        ],
    )
    return [nl, us]


LABEL = "📱 Очень длинное имя устройства для проверки лимита"
KEY = vd.device_key(LABEL)


class Link(FakeNodeLink):
    def __init__(self, servers, **kw):
        super().__init__(**kw)
        self.servers = servers

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        self.dsts.append(dst)
        if action == vpn_protocol.ACTION_REISSUE:
            return {
                "device_label": args["device_label"],
                "transport": args["transport"],
                "config_text": "cfg",
                "qr_png_b64": None,
                "location": "🇺🇸 США",
                "prior_device_count": 1,
            }
        return {}


@pytest.fixture(autouse=True)
def _fanout(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn_nodes, "KNOWN_SERVERS_PATH", tmp_path / "known.json")
    state = {"servers": _servers()}

    async def fake_fanout(node_link, action, args):
        return state["servers"]

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    return state


def _all_buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _texts(markup):
    return [b.text for b in _all_buttons(markup)]


async def _press(data, sub=GUEST_FULL, link=None, chat_id=778):
    link = link or Link(None)
    notifier = FakeNotifier()
    cb = FakeCallback(data, chat_id=chat_id)
    pend = PendingVpnSecrets()
    await h.handle_action(cb, link, notifier, _config(), sub, pend)
    return cb, link, notifier


def test_all_callbacks_fit_64_bytes():
    servers = _servers()
    device = vd.build_devices(servers)[0]
    markups = [
        h._home_keyboard(servers, subscription=ADMIN, self_serve_nodes=["jeeves", "wooster"]),
        h._list_keyboard(vd.build_devices(servers), subscription=ADMIN),
        h._card_keyboard_for_device(device, servers, subscription=ADMIN),
        h._reissue_keyboard(device, servers),
        h._delete_keyboard(device),
    ]
    for markup in markups:
        for button in _all_buttons(markup):
            assert len(button.callback_data.encode()) <= 64, button.callback_data


async def test_home_for_user_with_devices(_fanout):
    cb, _, _ = await _press("act:" + "vpn:vpn_card")
    text, markup = cb.message.edits[-1], cb.message.edit_markups[-1]
    assert text.startswith("📶 <b>VPN</b>")
    assert "🇳🇱 Нидерланды: осталось 87 ГБ из 100 до " in text
    flat = _texts(markup)
    assert "⚙️ Управление" in flat and "❓ Помощь" in flat and "➕ Новое устройство" in flat
    assert not any("Подключить по шагам" in t or "Сообщить" in t for t in flat)
    assert not any("Перевыпустить" in t or "Отозвать" in t for t in flat)


async def test_home_without_devices_is_old_card(_fanout):
    _fanout["servers"] = [_srv("jeeves", "", [], devices=[])]
    cb, _, _ = await _press("act:vpn:vpn_card")
    assert "📶 <b>VPN</b>" in cb.message.edits[-1]
    assert "Управление" not in " ".join(_texts(cb.message.edit_markups[-1]))


async def test_home_shows_warning_in_words(_fanout):
    _fanout["servers"][1]["check"] = [{"transport": "reality", "status": "alerting"}]
    _fanout["servers"][0]["check"] = [{"transport": "reality", "status": "ok"}]
    cb, _, _ = await _press("act:vpn:vpn_card")
    assert "⚠️ США сейчас может не работать — выберите Нидерланды." in cb.message.edits[-1]


async def test_list_screen():
    cb, _, _ = await _press(f"act:vpn:vpn_card:{h._SCREEN_LIST}")
    text, markup = cb.message.edits[-1], cb.message.edit_markups[-1]
    assert text.startswith("⚙️ <b>Мои устройства</b>")
    assert "9.1 ГБ, на связи 10.10" in text
    texts = _texts(markup)
    assert texts[0].startswith("📱 Очень длинное")
    assert "➕ Новое устройство" in texts and "⬅️ Назад" in texts


async def test_device_card_with_fix_button_for_broken_country():
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    text, markup = cb.message.edits[-1], cb.message.edit_markups[-1]
    assert "🇳🇱 Нидерланды — 8.7 ГБ" in text
    assert "🇺🇸 AmneziaWG — 🔧 сервер его не помнит" in text
    texts = _texts(markup)
    assert "🔧 Починить 🇺🇸" in texts
    assert "🔄 Перевыпустить ключи" in texts and "🗑 Удалить устройство" in texts
    assert "⬅️ К устройствам" in texts
    assert not any("Починить 🇳🇱" in t for t in texts)


async def test_device_card_without_broken_has_no_fix(_fanout):
    _fanout["servers"] = _servers(broken=False)
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    assert not any("Починить" in t for t in _texts(cb.message.edit_markups[-1]))


async def test_card_buttons_follow_rights():
    view_only = GUEST_FULL.__class__(
        chat_id=779, name="v", allowed_commands=frozenset({"usage@vpn", "vpn_card@vpn"})
    )
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}", sub=view_only, chat_id=779)
    assert _texts(cb.message.edit_markups[-1]) == ["⬅️ К устройствам"]


async def test_unknown_device_goes_back_to_list():
    cb, _, _ = await _press("act:vpn:vpn_card:dffffffff")
    assert cb.answered[0][1].get("show_alert")
    assert cb.message.edits[-1].startswith("⚙️ <b>Мои устройства</b>")


async def test_delete_asks_confirmation_first():
    cb, link, _ = await _press(f"act:vpn:vpn_card:x{KEY}")
    assert "Удалить" in cb.message.edits[-1] and "во всех странах" in cb.message.edits[-1]
    assert _texts(cb.message.edit_markups[-1]) == ["Да, удалить", "Отмена"]
    assert not [c for c in link.calls if c[0] == vpn_protocol.ACTION_REVOKE]


async def test_delete_revokes_every_node_and_transport():
    cb, link, _ = await _press(f"act:vpn:revoke:~d{KEY}")
    revokes = [
        (d.node, a["transport"], a["device_label"])
        for (act, a), d in zip(link.calls, link.dsts, strict=True)
        if act == vpn_protocol.ACTION_REVOKE
    ]
    assert sorted(revokes) == sorted(
        [("jeeves", "reality", LABEL), ("wooster", "reality", LABEL), ("wooster", "awg", LABEL)]
    )
    assert all(a["chat_id"] == 778 for act, a in link.calls if act == vpn_protocol.ACTION_REVOKE)


async def test_delete_partial_failure_keeps_device_screen():
    class Flaky(Link):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            if action == vpn_protocol.ACTION_REVOKE and dst.node == "wooster":
                raise ProtoError("bad_request", "нет")
            return await super().command(action, args, dst)

    cb, _, _ = await _press(f"act:vpn:revoke:~d{KEY}", link=Flaky(None))
    assert cb.answered[0][1].get("show_alert")
    assert cb.message.edits[-1].startswith(f"<b>{LABEL}")


async def test_fix_reissues_only_broken_in_that_country_and_delivers_secret():
    cb, link, notifier = await _press(f"act:vpn:reissue:~f{KEY}:wooster")
    reissues = [
        (d.node, a)
        for (act, a), d in zip(link.calls, link.dsts, strict=True)
        if act == vpn_protocol.ACTION_REISSUE
    ]
    assert reissues == [("wooster", {"chat_id": 778, "device_label": LABEL, "transport": "awg"})]
    assert notifier.sent_direct  # _send_secret отработал: кнопка «Дать файл конфига»
    assert cb.message.edits  # карточка перерисована


async def test_fix_in_group_chat_refused():
    cb, link, _ = await _press(f"act:vpn:reissue:~f{KEY}:wooster", chat_id=-100)
    assert not [c for c in link.calls if c[0] == vpn_protocol.ACTION_REISSUE]
    assert cb.answered[0][1].get("show_alert")


async def test_reissue_menu_lists_only_issued_and_reissues_one():
    cb, _, _ = await _press(f"act:vpn:vpn_card:r{KEY}")
    texts = _texts(cb.message.edit_markups[-1])
    assert texts[:-1] == [
        "🔄 🇳🇱 VLESS · Hiddify",
        "🔄 🇺🇸 VLESS · Hiddify",
        "🔄 🇺🇸 AmneziaWG",
    ]
    cb, link, _ = await _press(f"act:vpn:reissue:~r{KEY}:jeeves")
    reissue = [a for act, a in link.calls if act == vpn_protocol.ACTION_REISSUE]
    assert reissue == [{"chat_id": 778, "device_label": LABEL, "transport": "reality"}]


async def test_new_device_and_help_lead_to_existing_flows():
    cb, _, _ = await _press("act:vpn:vpn_card")
    buttons = {b.text: b.callback_data for b in _all_buttons(cb.message.edit_markups[-1])}
    assert buttons["➕ Новое устройство"] == commands.action_callback("issue", service="vpn")
    assert buttons["❓ Помощь"] == commands.action_callback("apk", service="vpn")


async def test_probe_chat_zero_never_requested(monkeypatch):
    asked = []

    async def spy(node_link, action, args):
        asked.append(args)
        return _servers()

    monkeypatch.setattr(vpn_nodes, "fanout", spy)
    await _press("act:vpn:vpn_card:m")
    assert all(a.get("chat_id") == 778 for a in asked)
