from __future__ import annotations

import pytest

from sa_home_bot.bot import commands, vpn_callbacks, vpn_nodes, vpn_screens
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_settings as vs
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

from .vpn_support import (
    ADMIN,
    GB,
    GUEST_FULL,
    KEY,
    LABEL,
    FakeNodeLink,
    all_buttons,
    button_texts,
    make_conn,
    make_srv,
    press,
)


def _servers(*, broken=True):
    nl = make_srv(
        "jeeves",
        "🇳🇱 Нидерланды",
        [
            {
                "device_label": "📱 Очень длинное имя устройства для проверки лимита",
                "used_bytes": int(8.7 * GB),
                "connections": [make_conn("reality", "2026-10-10T14:02:00+00:00")],
            }
        ],
    )
    us = make_srv(
        "wooster",
        "🇺🇸 США",
        [
            {
                "device_label": "📱 Очень длинное имя устройства для проверки лимита",
                "used_bytes": int(0.4 * GB),
                "connections": [make_conn("reality"), make_conn("awg", broken=broken)],
            }
        ],
    )
    return [nl, us]


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
def _fanout(monkeypatch):
    state = {"servers": _servers()}

    async def fake_fanout(node_link, action, args):
        return state["servers"]

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    return state


async def _press(data, sub=GUEST_FULL, link=None, chat_id=778):
    return await press(data, sub=sub, link=link or Link(None), chat_id=chat_id)


def test_all_callbacks_fit_64_bytes():
    servers = _servers()
    device = vd.build_devices(servers)[0]
    markups = [
        vpn_screens.home_keyboard(
            servers, subscription=ADMIN, self_serve_nodes=["jeeves", "wooster"]
        ),
        vpn_screens.list_keyboard(vd.build_devices(servers), subscription=ADMIN),
        vpn_screens.device_card_keyboard(device, servers, subscription=ADMIN),
        vs.reissue_select_keyboard(
            device, vs.reissue_connections(device), vd.countries_of(servers), 5
        ),
        vpn_screens.delete_keyboard(device),
    ]
    for markup in markups:
        for button in all_buttons(markup):
            assert len(button.callback_data.encode()) <= 64, button.callback_data


async def test_home_for_user_with_devices(_fanout):
    cb, _, _ = await _press("act:" + "vpn:vpn_card")
    text, markup = cb.message.edits[-1], cb.message.edit_markups[-1]
    assert text.startswith("📶 <b>VPN</b>")
    assert "🇳🇱 Нидерланды: осталось 87 ГБ из 100 до " in text
    flat = button_texts(markup)
    assert "⚙️ Управление устройствами" in flat and "❓ Помощь" in flat
    assert "➕ Новое устройство" not in flat and "📱 Подключить по шагам" not in flat
    assert "⚠️ Сообщить о проблеме" in flat
    assert not any("Перевыпустить" in t or "Отозвать" in t for t in flat)


async def test_home_without_devices_is_old_card_in_group(_fanout):
    _fanout["servers"] = [make_srv("jeeves", "", [], devices=[])]
    cb, _, _ = await _press("act:vpn:vpn_card", chat_id=-100)
    assert "📶 <b>VPN</b>" in cb.message.edits[-1]
    assert "Управление" not in " ".join(button_texts(cb.message.edit_markups[-1]))


async def test_home_shows_warning_in_words(_fanout):
    _fanout["servers"][1]["check"] = [{"transport": "reality", "status": "alerting"}]
    _fanout["servers"][0]["check"] = [{"transport": "reality", "status": "ok"}]
    cb, _, _ = await _press("act:vpn:vpn_card")
    text = cb.message.edits[-1]
    assert "🇺🇸 США — ⚠️ может не работать" in text
    assert "🇳🇱 Нидерланды — ✅ работает" in text
    assert "Если одна страна не работает — выберите другую." in text


async def test_list_screen():
    cb, _, _ = await _press(f"act:vpn:vpn_card:{vpn_callbacks.SCREEN_LIST}")
    text, markup = cb.message.edits[-1], cb.message.edit_markups[-1]
    assert text.startswith("⚙️ <b>Мои устройства</b>")
    assert "9.1 ГБ, на связи 10.10" in text
    texts = button_texts(markup)
    assert texts[0].startswith("📱 Очень длинное")
    assert "➕ Новое устройство" in texts and "⬅️ Назад" in texts


async def test_device_card_with_fix_button_for_broken_country():
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    text, markup = cb.message.edits[-1], cb.message.edit_markups[-1]
    assert "🇳🇱 Нидерланды — 8.7 ГБ" in text
    assert "🇺🇸 AmneziaVPN — 🔧 сервер его не помнит" in text
    texts = button_texts(markup)
    assert "🔧 Починить 🇺🇸" in texts
    assert "🔄 Перевыпустить ключи" in texts and "🗑 Удалить устройство" in texts
    assert "⬅️ К устройствам" in texts
    assert not any("Починить 🇳🇱" in t for t in texts)


async def test_device_card_without_broken_has_no_fix(_fanout):
    _fanout["servers"] = _servers(broken=False)
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    assert not any("Починить" in t for t in button_texts(cb.message.edit_markups[-1]))


async def test_card_buttons_follow_rights():
    view_only = GUEST_FULL.__class__(
        chat_id=779, name="v", allowed_commands=frozenset({"usage@vpn", "vpn_card@vpn"})
    )
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}", sub=view_only, chat_id=779)
    assert button_texts(cb.message.edit_markups[-1]) == ["⚠️ Сообщить о проблеме", "⬅️ К устройствам"]


async def test_unknown_device_goes_back_to_list():
    cb, _, _ = await _press("act:vpn:vpn_card:dffffffff")
    assert cb.answered[0][1].get("show_alert")
    assert cb.message.edits[-1].startswith("⚙️ <b>Мои устройства</b>")


async def test_delete_asks_confirmation_first():
    cb, link, _ = await _press(f"act:vpn:vpn_card:x{KEY}")
    assert "Удалить" in cb.message.edits[-1] and "во всех странах" in cb.message.edits[-1]
    assert button_texts(cb.message.edit_markups[-1]) == ["Да, удалить", "Отмена"]
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
    assert notifier.sent
    assert cb.message.edits


async def test_fix_in_group_chat_refused():
    cb, link, _ = await _press(f"act:vpn:reissue:~f{KEY}:wooster", chat_id=-100)
    assert not [c for c in link.calls if c[0] == vpn_protocol.ACTION_REISSUE]
    assert cb.answered[0][1].get("show_alert")


async def test_reissue_menu_lists_only_issued_unchecked():
    cb, _, _ = await _press(f"act:vpn:vpn_card:r{KEY}")
    texts = button_texts(cb.message.edit_markups[-1])
    assert texts == [
        "☐ 🇳🇱 VLESS · Hiddify",
        "☐ 🇺🇸 AmneziaVPN",
        "☐ 🇺🇸 VLESS · Hiddify",
        "Выбрать все",
        "Отмена",
    ]


async def test_new_device_and_help_buttons():
    cb, _, _ = await _press("act:vpn:vpn_card")
    buttons = {b.text: b.callback_data for b in all_buttons(cb.message.edit_markups[-1])}
    assert buttons["❓ Помощь"] == commands.action_callback("apk", service="vpn")
    cb, _, _ = await _press("act:vpn:vpn_card:m")
    buttons = {b.text: b.callback_data for b in all_buttons(cb.message.edit_markups[-1])}
    assert buttons["➕ Новое устройство"] == commands.action_callback(
        "vpn_card", "n", service="vpn"
    )


async def test_probe_chat_zero_never_requested(monkeypatch):
    asked = []

    async def spy(node_link, action, args):
        asked.append(args)
        return _servers()

    monkeypatch.setattr(vpn_nodes, "fanout", spy)
    await _press("act:vpn:vpn_card:m")
    assert all(a.get("chat_id") == 778 for a in asked)
