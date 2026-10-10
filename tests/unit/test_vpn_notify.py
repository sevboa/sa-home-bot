"""Этап 57.7: VPN-уведомления со страной, «сервер переустановлен», без «jeeves»."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_notify, vpn_settings
from sa_home_bot.bot.node_events import build_node_event_handler
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.proto.messages import Address, make_event
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.vpn import protocol as vp

NL = "🇳🇱 Нидерланды"
US = "🇺🇸 США"
ADMIN = 999
GUEST = 111
OTHER = 222


class Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []

    async def send_direct(self, chat_id, text, reply_markup=None, **_kw):
        self.sent.append((chat_id, text, reply_markup))
        return 1


class Store:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def record_event(self, *args):
        self.events.append(args)


class Link:
    """Ответы usage другой страны (для «Другие страны работают»)."""

    def __init__(self, servers=()):
        self.servers = list(servers)

    async def request(self, *a, **kw):  # pragma: no cover — fanout идёт через vpn_nodes
        raise AssertionError


def _book():
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="admin", chat_id=ADMIN, allowed_commands=["*"])]
    )


def _run(monkeypatch, servers=()):
    async def fake_fanout(link, action, args):
        return list(servers)

    monkeypatch.setattr("sa_home_bot.bot.vpn_nodes.fanout", fake_fanout)
    notifier, store = Notifier(), Store()
    handler = build_node_event_handler(_book(), notifier, store, get_node_link=lambda: object())

    async def send(name, data, node="jeeves"):
        await handler(make_event(name, data, src=Address(node=node, service="vpn")))

    return notifier, store, send


def _buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


# --- определение страны ------------------------------------------------------


def test_country_from_event_location():
    c = vpn_notify.country_of({"node": "jeeves", "location": NL})
    assert (c.node, c.flag, c.name) == ("jeeves", "🇳🇱", "Нидерланды")


def test_country_falls_back_to_remembered_label(monkeypatch):
    monkeypatch.setattr("sa_home_bot.bot.vpn_nodes._load_known", lambda: {"wooster": US})
    assert vpn_notify.country_of({"node": "wooster"}).flag == "🇺🇸"


def test_country_unknown_has_no_flag(monkeypatch):
    monkeypatch.setattr("sa_home_bot.bot.vpn_nodes._load_known", lambda: {})
    c = vpn_notify.country_of({"node": "x1"})
    assert vpn_notify.tag(c) == "VPN" and vpn_notify.in_country(c) == ""


# --- тексты квоты ------------------------------------------------------------


def test_quota_texts():
    nl = vd.Country("jeeves", NL)
    assert vpn_notify.quota_warning_text(nl, 4_200_000_000) == (
        "📶 VPN 🇳🇱: осталось 4.2 ГБ до конца месяца."
    )
    assert vpn_notify.quota_exceeded_text(nl, others_work=True) == (
        "⛔️ VPN 🇳🇱: лимит месяца исчерпан, в Нидерландах связь приостановлена. "
        "Другие страны работают."
    )
    assert vpn_notify.quota_exceeded_text(nl, others_work=False) == (
        "⛔️ VPN 🇳🇱: лимит месяца исчерпан, в Нидерландах связь приостановлена."
    )
    assert vpn_notify.quota_exceeded_text(vd.Country("wooster", US), others_work=False).count(
        "в США"
    )
    assert vpn_notify.access_restored_text(nl) == "✅ VPN 🇳🇱: снова работает."


def test_grant_button_has_flag_and_node():
    [(text, cb)] = _buttons(vpn_notify.grant_keyboard(vd.Country("jeeves", NL)))
    assert text == "➕ 100 ГБ 🇳🇱" and cb.endswith(":jeeves") and "grant_extra" in cb


def test_others_with_quota():
    other = {"node": "wooster", "allowed": True, "blocked": False, "remaining_bytes": 5}
    assert vpn_notify.others_with_quota([other], "jeeves")
    assert not vpn_notify.others_with_quota([{**other, "blocked": True}], "jeeves")
    assert not vpn_notify.others_with_quota([{**other, "remaining_bytes": 0}], "jeeves")
    assert not vpn_notify.others_with_quota([{**other, "allowed": False}], "jeeves")
    assert not vpn_notify.others_with_quota([{**other, "node": "jeeves"}], "jeeves")


async def test_quota_events_go_to_guest_with_country(monkeypatch):
    notifier, _store, send = _run(monkeypatch)
    where = {"chat_id": GUEST, "node": "jeeves", "location": NL}
    await send(vp.EVENT_VPN_QUOTA_WARNING, {**where, "remaining_bytes": 4_200_000_000})
    await send(vp.EVENT_VPN_ACCESS_RESTORED, where)
    await send(vp.EVENT_VPN_EXTRA_RESOLVED, {**where, "approved": True})
    await send(vp.EVENT_VPN_EXTRA_RESOLVED, {**where, "approved": False})
    texts = [t for _c, t, _m in notifier.sent]
    assert texts == [
        "📶 VPN 🇳🇱: осталось 4.2 ГБ до конца месяца.",
        "✅ VPN 🇳🇱: снова работает.",
        "✅ VPN 🇳🇱: заявка на доп. трафик одобрена.",
        "🚫 VPN 🇳🇱: заявка на доп. трафик отклонена.",
    ]
    assert {c for c, _t, _m in notifier.sent} == {GUEST}
    assert _buttons(notifier.sent[0][2])[0][0] == "➕ 100 ГБ 🇳🇱"


async def test_quota_exceeded_mentions_other_countries_only_when_they_work(monkeypatch):
    working = {"node": "wooster", "allowed": True, "blocked": False, "remaining_bytes": 10**9}
    notifier, _s, send = _run(monkeypatch, [working])
    data = {"chat_id": GUEST, "node": "jeeves", "location": NL}
    await send(vp.EVENT_VPN_QUOTA_EXCEEDED, data)
    assert notifier.sent[0][1].endswith("Другие страны работают.")
    notifier, _s, send = _run(monkeypatch, [{**working, "blocked": True}])
    await send(vp.EVENT_VPN_QUOTA_EXCEEDED, data)
    assert "Другие страны" not in notifier.sent[0][1]


async def test_probe_chat_zero_gets_nothing(monkeypatch):
    notifier, store, send = _run(monkeypatch)
    for name in (
        vp.EVENT_VPN_QUOTA_WARNING,
        vp.EVENT_VPN_QUOTA_EXCEEDED,
        vp.EVENT_VPN_ACCESS_RESTORED,
        vp.EVENT_VPN_EXTRA_RESOLVED,
        vp.EVENT_VPN_PEER_ISSUED,
        vp.EVENT_VPN_EXTRA_REQUESTED,
    ):
        await send(name, {"chat_id": 0, "node": "jeeves", "location": NL, "request_id": 1})
    assert notifier.sent == [] and store.events == []


# --- владельцу ---------------------------------------------------------------


async def test_node_quota_warning_has_no_hardcoded_jeeves(monkeypatch):
    notifier, store, send = _run(monkeypatch)
    data = {"used_bytes": 800 * 10**9, "limit_bytes": 1000 * 10**9}
    await send(vp.EVENT_VPN_NODE_QUOTA_WARNING, {**data, "node": "wooster", "location": US})
    [(chat, text, _m)] = notifier.sent
    assert chat == ADMIN
    assert text == "⚠️ VPN 🇺🇸 США: канал близок к месячному лимиту тарифа — 800 / 1000 ГБ."
    assert "jeeves" not in text
    assert "jeeves" not in store.events[0][2]


async def test_node_quota_warning_without_location_names_node(monkeypatch):
    monkeypatch.setattr("sa_home_bot.bot.vpn_nodes._load_known", lambda: {})
    notifier, _s, send = _run(monkeypatch)
    await send(vp.EVENT_VPN_NODE_QUOTA_WARNING, {"used_bytes": 1, "limit_bytes": 2, "node": "w9"})
    assert "w9" in notifier.sent[0][1]


async def test_admin_texts_carry_country(monkeypatch):
    notifier, _s, send = _run(monkeypatch)
    where = {"node": "wooster", "location": US}
    await send(
        vp.EVENT_VPN_EXTRA_REQUESTED, {"chat_id": GUEST, "bytes": 10**11, "request_id": 7, **where}
    )
    await send(
        vp.EVENT_VPN_PEER_ISSUED, {"chat_id": GUEST, "device_label": "📱 iPhone", **where}
    )
    assert notifier.sent[0][1].startswith("✋ VPN 🇺🇸: гость")
    assert notifier.sent[1][1] == f"🔐 VPN 🇺🇸: выдан доступ гостю <code>{GUEST}</code> (📱 iPhone)."


# --- новый доступ ------------------------------------------------------------


def test_access_opened_texts():
    us = vd.Country("wooster", US)
    plain = vpn_notify.access_opened_text(us, 100, has_devices=False)
    assert plain == "📶 Вам открыт VPN: 🇺🇸 США, 100 ГБ в месяц."
    withdev = vpn_notify.access_opened_text(us, 100, has_devices=True)
    assert withdev.startswith(plain)
    assert "Страна появится в Hiddify сама" in withdev
    assert "«📥 Получить настройки» в «⚙️ Управление»" in withdev
    assert vpn_notify.access_closed_text(us) == "📶 VPN 🇺🇸: доступ закрыт."


def test_access_keyboard_wizard_or_home():
    [(text, _)] = _buttons(vpn_notify.access_keyboard(has_devices=False))
    assert text == "📱 Подключить по шагам"
    [(text, cb)] = _buttons(vpn_notify.access_keyboard(has_devices=True))
    assert text == "📶 Открыть VPN" and cb.endswith("vpn_card")
    [(text, _)] = _buttons(vpn_notify.access_keyboard(has_devices=None))
    assert text == "📶 Открыть VPN"


async def test_notify_guest_access_handler(monkeypatch):
    from sa_home_bot.bot.handlers import vpn as handler

    async def fake_card(link, chat_id, subscription=None):
        return None, servers, []

    monkeypatch.setattr(handler, "_card", fake_card)
    notifier = Notifier()
    server = {"node": "wooster", "label": US, "base_limit_bytes": 100 * 10**9, "allowed": True}
    servers = []
    await handler._notify_guest_access(notifier, GUEST, server, opened=True, node_link=object())
    assert notifier.sent[0][1] == "📶 Вам открыт VPN: 🇺🇸 США, 100 ГБ в месяц."
    assert _buttons(notifier.sent[0][2])[0][0] == "📱 Подключить по шагам"
    servers = [
        {
            "node": "jeeves",
            "label": NL,
            "devices": [
                {"device_label": "📱 iPhone", "transport": "reality", "status": "active"}
            ],
        }
    ]
    notifier = Notifier()
    monkeypatch.setattr(
        handler.vpn_devices, "build_devices", lambda s: [object()] if s else []
    )
    await handler._notify_guest_access(notifier, GUEST, server, opened=True, node_link=object())
    assert "Страна появится в Hiddify сама" in notifier.sent[0][1]
    assert _buttons(notifier.sent[0][2])[0][0] == "📶 Открыть VPN"
    notifier = Notifier()
    await handler._notify_guest_access(notifier, GUEST, server, opened=False)
    assert notifier.sent[0][1] == "📶 VPN 🇺🇸: доступ закрыт."
    notifier = Notifier()
    await handler._notify_guest_access(notifier, -5, server, opened=True)  # не личка
    assert notifier.sent == []


# --- сервер переустановлен ---------------------------------------------------


def _item(chat, label, transport):
    return {"chat_id": chat, "device_label": label, "transport": transport}


async def test_restore_one_device_goes_to_reissue_screen(monkeypatch):
    notifier, _s, send = _run(monkeypatch)
    data = {
        "node": "jeeves",
        "location": NL,
        "affected": [_item(GUEST, "📱 iPhone", "awg"), _item(GUEST, "📱 iPhone", "reality")],
    }
    await send(vp.EVENT_VPN_SERVER_RESTORED, data)
    guest = [m for m in notifier.sent if m[0] == GUEST]
    assert len(guest) == 1
    assert guest[0][1] == (
        "🔧 VPN 🇳🇱: сервер переустановлен, настройки нужно обновить: "
        "📱 iPhone (AmneziaVPN, VLESS · Hiddify).\n"
        "Если подключались кнопкой «🔌 Подключить», Hiddify обновится сам."
    )
    [(text, cb)] = _buttons(guest[0][2])
    key, nh, codes = vpn_settings.parse_restore(cb.split(":")[-1][1:])
    assert text == "Обновить" and cb.startswith("act:vpn:vpn_card:R")
    assert key == vd.device_key("📱 iPhone") and nh == vpn_settings.node_hash("jeeves")
    assert codes == "av" and len(cb.encode()) <= 64
    owner = [m for m in notifier.sent if m[0] == ADMIN]
    assert owner[0][1] == "🔧 VPN 🇳🇱: сервер переустановлен, задето 2 подключения у 1 человека."


async def test_restore_awg_only_has_no_hiddify_hint(monkeypatch):
    notifier, _s, send = _run(monkeypatch)
    await send(
        vp.EVENT_VPN_SERVER_RESTORED,
        {"node": "jeeves", "location": NL, "affected": [_item(GUEST, "📱 iPhone", "awg")]},
    )
    assert "Hiddify обновится" not in notifier.sent[0][1]
    assert "📱 iPhone (AmneziaVPN)" in notifier.sent[0][1]


async def test_restore_several_devices_leads_to_manage_list(monkeypatch):
    notifier, _s, send = _run(monkeypatch)
    data = {
        "node": "jeeves",
        "location": NL,
        "affected": [
            _item(GUEST, "📱 iPhone", "awg"),
            _item(GUEST, "🌸 Rose", "reality"),
            _item(OTHER, "💻 Компьютер", "reality"),
        ],
    }
    await send(vp.EVENT_VPN_SERVER_RESTORED, data)
    by_chat = {c: (t, m) for c, t, m in notifier.sent}
    text, markup = by_chat[GUEST]
    assert "📱 iPhone (AmneziaVPN), 🌸 Rose (VLESS · Hiddify)" in text
    assert _buttons(markup) == [("Обновить", vpn_settings.card_cb("m"))]
    assert "Hiddify обновится сам" in by_chat[OTHER][0]
    assert by_chat[ADMIN][0] == (
        "🔧 VPN 🇳🇱: сервер переустановлен, задето 3 подключения у 2 человек."
    )
    assert len([m for m in notifier.sent if m[0] in (GUEST, OTHER)]) == 2


async def test_restore_empty_affected_only_owner_line(monkeypatch):
    notifier, store, send = _run(monkeypatch)
    await send(vp.EVENT_VPN_SERVER_RESTORED, {"node": "jeeves", "location": NL, "affected": []})
    assert [(c, t) for c, t, _m in notifier.sent] == [
        (ADMIN, "🔧 VPN 🇳🇱: ключ сервера сменился, задетых нет.")
    ]
    assert store.events


async def test_restore_ignores_probe_chat_zero(monkeypatch):
    notifier, _s, send = _run(monkeypatch)
    await send(
        vp.EVENT_VPN_SERVER_RESTORED,
        {"node": "jeeves", "location": NL, "affected": [_item(0, "probe", "reality")]},
    )
    assert [c for c, _t, _m in notifier.sent] == [ADMIN]
    assert "задетых нет" in notifier.sent[0][1]
    notifier, _s, send = _run(monkeypatch)
    await send(
        vp.EVENT_VPN_SERVER_RESTORED,
        {
            "node": "jeeves",
            "location": NL,
            "affected": [_item(0, "probe", "reality"), _item(GUEST, "📱 iPhone", "awg")],
        },
    )
    assert sorted(c for c, _t, _m in notifier.sent) == [GUEST, ADMIN]
    assert "задето 1 подключение у 1 человека" in notifier.sent[-1][1]


async def test_restore_one_failed_delivery_does_not_block_others(monkeypatch):
    notifier, _s, send = _run(monkeypatch)
    real = notifier.send_direct

    async def flaky(chat_id, text, reply_markup=None, **kw):
        if chat_id == GUEST:
            raise RuntimeError("blocked")
        return await real(chat_id, text, reply_markup=reply_markup)

    notifier.send_direct = flaky
    await send(
        vp.EVENT_VPN_SERVER_RESTORED,
        {
            "node": "jeeves",
            "location": NL,
            "affected": [_item(GUEST, "a", "awg"), _item(OTHER, "b", "awg")],
        },
    )
    assert sorted(c for c, _t, _m in notifier.sent) == [OTHER, ADMIN]


# --- предвыбор на экране перевыпуска ----------------------------------------


def _conn(node, transport):
    return vd.Connection(node, transport, "active", None, None, False, 0)


def test_restore_mask_selects_only_affected_of_that_country():
    conns = [
        _conn("jeeves", "reality"),
        _conn("jeeves", "awg"),
        _conn("wooster", "reality"),
        _conn("wooster", "awg"),
    ]
    nh = vpn_settings.node_hash("jeeves")
    assert vpn_settings.restore_mask(conns, nh, "va") == 0b0011
    assert vpn_settings.restore_mask(conns, nh, "a") == 0b0010
    assert vpn_settings.restore_mask(conns, vpn_settings.node_hash("wooster"), "v") == 0b0100
    assert vpn_settings.restore_mask(conns, "zzzz", "va") == 0


def test_plural_forms():
    for n, form in ((1, "подключение"), (2, "подключения"), (5, "подключений"), (11, "подключений")):
        text = vpn_notify.restored_owner_text(
            vd.Country("j", NL), [_item(i + 1, "d", "awg") for i in range(n)]
        )
        assert f"{n} {form} " in text


def test_no_jeeves_in_any_text():
    for c in (vd.Country("wooster", US), vd.Country("x", "")):
        texts = [
            vpn_notify.quota_warning_text(c, 1),
            vpn_notify.quota_exceeded_text(c, others_work=True),
            vpn_notify.access_restored_text(c),
            vpn_notify.node_quota_text(c, 1, 2),
            vpn_notify.restored_owner_text(c, []),
            vpn_notify.access_opened_text(c, 100, has_devices=True),
        ]
        assert not any("jeeves" in t for t in texts)
