"""bot/handlers/vpn.py + bot/vpn_settings.py: выдача настроек в экспертном
режиме (57.4) — VLESS · Hiddify, AmneziaWG, новое устройство."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_nodes, vpn_settings
from sa_home_bot.bot.handlers import vpn as h
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

from .test_vpn_expert import _all_buttons, _conn, _srv, _texts
from .test_vpn_handler import GUEST_FULL, FakeCallback, FakeNodeLink, FakeNotifier

LABEL = "📱 iPhone"
KEY = vd.device_key(LABEL)
PAGE = "https://1.2.3.4:8444/s/tok"
HS = "2026-10-10T14:02:00+00:00"


class Link(FakeNodeLink):
    def __init__(self, *, fail_issue_on=()):
        super().__init__()
        self.fail_issue_on = set(fail_issue_on)

    def of(self, action):
        return [
            (a, d.node) for (act, a), d in zip(self.calls, self.dsts, strict=True) if act == action
        ]

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        self.dsts.append(dst)
        flag = "🇳🇱" if dst is not None and dst.node == "jeeves" else "🇺🇸"
        if action in (vpn_protocol.ACTION_ISSUE, vpn_protocol.ACTION_REISSUE):
            if dst.node in self.fail_issue_on:
                raise ProtoError("bad_request", "нет")
            return {
                "device_label": args["device_label"],
                "transport": args["transport"],
                "config_text": "[Interface]\nPrivateKey = x",
                "qr_png_b64": "UE5H",
                "location": f"{flag} страна",
            }
        if action == vpn_protocol.ACTION_GET_VLESS:
            return {
                "config_text": '{"singbox": 1}',
                "share_url": f"vless://uuid-{dst.node}@1.2.3.4:443#{flag}",
                "deep_link": "hiddify://import/x",
                "qr_png_b64": "UE5H",
                "location": f"{flag} страна",
            }
        return {}


def _servers(*, nl=("reality",), us=("reality",), hs=None, awg_check=None):
    def entry(conns):
        return [{"device_label": LABEL, "used_bytes": 0, "connections": conns}] if conns else []

    def conns(kinds):
        return [_conn(k, hs if k == "reality" else None) for k in kinds]

    jeeves = _srv("jeeves", "🇳🇱 Нидерланды", entry(conns(nl)), transports=["reality", "awg"])
    wooster = _srv("wooster", "🇺🇸 США", entry(conns(us)), transports=["reality", "awg"])
    if awg_check:
        wooster["check"] = [{"transport": "awg", "status": awg_check}]
    return [jeeves, wooster]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn_nodes, "KNOWN_SERVERS_PATH", tmp_path / "known.json")
    state = {"servers": _servers(), "page": PAGE}

    async def fake_fanout(node_link, action, args):
        if action == vpn_protocol.ACTION_GET_SUBSCRIPTION:
            return [{"page_url": state["page"]}] if state["page"] else []
        return state["servers"]

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    return state


async def _press(data, *, link=None, notifier=None, pend=None, chat_id=778, cfg=None):
    link = link or Link()
    notifier = notifier or FakeNotifier()
    cb = FakeCallback(data, chat_id=chat_id)
    await h.handle_action(
        cb,
        link,
        notifier,
        cfg or Settings(vpn=VpnConfig()),
        GUEST_FULL,
        pend or PendingVpnSecrets(),
    )
    return cb, link, notifier


def _last(cb):
    return cb.message.edits[-1], cb.message.edit_markups[-1]


# --- карточка и выбор технологии --------------------------------------------


async def test_card_has_get_settings_button_and_pick_screen():
    cb, _, _ = await _press(f"act:vpn:vpn_card:d{KEY}")
    assert "📥 Получить настройки" in _texts(cb.message.edit_markups[-1])
    cb, _, _ = await _press(f"act:vpn:vpn_card:g{KEY}")
    text, markup = _last(cb)
    assert text == f"📥 <b>{LABEL}</b> — чем подключаться?"
    assert _texts(markup) == ["🛡 VLESS · Hiddify", "⚡ AmneziaWG", "⬅️ Назад"]


# --- VLESS · Hiddify ---------------------------------------------------------


async def test_vless_without_handshake_sends_file_and_one_message():
    cb, link, notifier = await _press(f"act:vpn:issue:~v{KEY}")
    assert not link.of(vpn_protocol.ACTION_ISSUE)  # обе страны уже есть
    assert len(notifier.sent_documents) == 1
    assert notifier.sent_documents[0][2] == vpn_settings.VLESS_FILE_CAPTION
    (_, text), markup = notifier.sent_direct[0], notifier.sent_direct_markups[0]
    buttons = _all_buttons(markup)
    assert buttons[0].text == "🔌 Подключить" and buttons[0].url == PAGE  # первым
    assert "⚙️ Файл настроек ещё раз" not in _texts(markup)
    assert _texts(markup)[-2:] == ["📷 QR 🇳🇱", "📷 QR 🇺🇸"]
    assert "<code>vless://uuid-jeeves@1.2.3.4:443#🇳🇱</code>" in text
    assert "<code>vless://uuid-wooster@1.2.3.4:443#🇺🇸</code>" in text
    assert "«+» → «Из буфера обмена»" in text
    assert "нажмите на файл выше" in text.lower()


async def test_vless_with_handshake_gives_button_instead_of_file(_env):
    _env["servers"] = _servers(hs=HS)
    cb, link, notifier = await _press(f"act:vpn:issue:~v{KEY}")
    assert notifier.sent_documents == []
    markup = notifier.sent_direct_markups[0]
    assert _texts(markup)[:2] == ["🔌 Подключить", "⚙️ Файл настроек ещё раз"]
    # кнопка «ещё раз» отдаёт файл
    again = _all_buttons(markup)[1].callback_data
    cb, _, notifier = await _press(again)
    assert len(notifier.sent_documents) == 1
    assert notifier.sent_documents[0][1] == b'{"singbox": 1}'


async def test_vless_adds_missing_country_before_delivery(_env):
    _env["servers"] = _servers(us=())
    cb, link, notifier = await _press(f"act:vpn:issue:~v{KEY}")
    assert link.of(vpn_protocol.ACTION_ISSUE) == [
        ({"chat_id": 778, "device_label": LABEL, "transport": "reality"}, "wooster")
    ]
    assert sorted(n for _, n in link.of(vpn_protocol.ACTION_GET_VLESS)) == ["jeeves", "wooster"]
    text = notifier.sent_direct[0][1]
    assert "➕ Добавлено: 🇺🇸 США." in text
    assert "uuid-wooster" in text


async def test_vless_survives_failed_extra_country(_env):
    _env["servers"] = _servers(us=())
    cb, link, notifier = await _press(
        f"act:vpn:issue:~v{KEY}", link=Link(fail_issue_on={"wooster"})
    )
    text = notifier.sent_direct[0][1]
    assert "uuid-jeeves" in text and "uuid-wooster" not in text and "Добавлено" not in text


async def test_vless_without_subscription_page_has_no_connect_button(_env):
    _env["page"] = None
    _, _, notifier = await _press(f"act:vpn:issue:~v{KEY}")
    assert "🔌 Подключить" not in _texts(notifier.sent_direct_markups[0])


async def test_vless_qr_buttons_are_independent_and_secrets_get_deleted():
    pend = PendingVpnSecrets()
    cb, link, notifier = await _press(f"act:vpn:issue:~v{KEY}", pend=pend)
    qr = [b for b in _all_buttons(notifier.sent_direct_markups[0]) if b.text.startswith("📷")]
    assert len(qr) == 2
    cb2, _, notifier2 = await _press(qr[0].callback_data, pend=pend)
    assert len(notifier2.sent_photos) == 1 and "🇳🇱" in notifier2.sent_photos[0][2]
    # нажатая кнопка уходит, остальное на месте
    from types import SimpleNamespace

    left = h._without_button(
        SimpleNamespace(reply_markup=notifier.sent_direct_markups[0]), qr[0].callback_data
    )
    assert _texts(left) == ["🔌 Подключить", "📷 QR 🇺🇸"]
    only = SimpleNamespace(reply_markup=left)
    assert h._without_button(only, qr[1].callback_data).inline_keyboard == [left.inline_keyboard[0]]


async def test_vless_refused_in_group_chat():
    cb, link, notifier = await _press(f"act:vpn:issue:~v{KEY}", chat_id=-100)
    assert not link.calls and not notifier.sent_direct
    assert cb.answered[0][1].get("show_alert")


async def test_settings_file_flag_off_means_no_file_and_no_button(_env):
    cfg = Settings(vpn=VpnConfig(wizard_settings_file=False))
    _, _, notifier = await _press(f"act:vpn:issue:~v{KEY}", cfg=cfg)
    assert notifier.sent_documents == []
    assert "⚙️ Файл настроек ещё раз" not in _texts(notifier.sent_direct_markups[0])


# --- AmneziaWG ---------------------------------------------------------------


async def test_awg_country_list_marks_failing_country_in_words(_env):
    _env["servers"] = _servers(awg_check="alerting")
    cb, _, _ = await _press(f"act:vpn:vpn_card:a{KEY}")
    assert _texts(cb.message.edit_markups[-1]) == [
        "🇳🇱 Нидерланды",
        "🇺🇸 США — сейчас может не работать",
        "⬅️ Назад",
    ]


async def test_awg_issue_sends_file_steps_and_buttons():
    cb, link, notifier = await _press(f"act:vpn:issue:~g{KEY}:jeeves")
    assert link.of(vpn_protocol.ACTION_ISSUE) == [
        ({"chat_id": 778, "device_label": LABEL, "transport": "awg"}, "jeeves")
    ]
    assert not link.of(vpn_protocol.ACTION_REISSUE)
    (_, doc, caption) = notifier.sent_documents[0]
    assert doc.startswith(b"[Interface]") and caption == f"⚡ {LABEL} · AmneziaWG · 🇳🇱"
    (_, text), markup = notifier.sent_direct[0], notifier.sent_direct_markups[0]
    assert "1. Установите именно AmneziaVPN" in text
    assert "«Открыть в AmneziaVPN»" in text
    assert f"❗️ Обязательно переименуйте добавленное подключение — например, «🇳🇱 {LABEL}»." in text
    texts = _texts(markup)
    assert texts == [
        "App Store",
        "Google Play",
        "Сайт",
        "📷 QR для другого устройства",
        "Магазин недоступен?",
    ]
    urls = [b.url for b in _all_buttons(markup) if b.url]
    cfg = VpnConfig()
    assert urls == [
        cfg.amneziavpn_ios_app_store_url,
        cfg.amneziavpn_google_play_url,
        cfg.official_download_url,
    ]
    assert "amneziawg" not in " ".join(urls).lower()


async def test_awg_existing_key_asks_before_replacing(_env):
    _env["servers"] = _servers(nl=("reality", "awg"))
    cb, link, notifier = await _press(f"act:vpn:issue:~g{KEY}:jeeves")
    text, markup = _last(cb)
    assert text == (
        f"⚠️ У <b>{LABEL}</b> уже есть ключ AmneziaWG 🇳🇱.\n"
        "Новый заменит старый — там, где стоит старый, связь пропадёт."
    )
    assert _texts(markup) == ["Выпустить новый", "Отмена"]
    assert not link.calls and not notifier.sent_documents  # пока ничего не выпущено
    # подтверждение → reissue именно awg на этой ноде
    cb, link, notifier = await _press(_all_buttons(markup)[0].callback_data)
    assert link.of(vpn_protocol.ACTION_REISSUE) == [
        ({"chat_id": 778, "device_label": LABEL, "transport": "awg"}, "jeeves")
    ]
    assert not link.of(vpn_protocol.ACTION_ISSUE)
    assert notifier.sent_documents and notifier.sent_direct
    assert cb.message.edits[-1].startswith("⚡")  # предупреждение заменено списком стран


async def test_awg_cancel_returns_to_country_list(_env):
    _env["servers"] = _servers(nl=("reality", "awg"))
    _, markup = _last((await _press(f"act:vpn:issue:~g{KEY}:jeeves"))[0])
    cb, link, _ = await _press(_all_buttons(markup)[1].callback_data)
    assert cb.message.edits[-1].startswith("⚡") and not link.calls


async def test_store_unavailable_text_and_apk_button():
    _, _, notifier = await _press(f"act:vpn:issue:~g{KEY}:jeeves")
    no_store = [
        b for b in _all_buttons(notifier.sent_direct_markups[0]) if b.text == "Магазин недоступен?"
    ][0]
    cb, _, _ = await _press(no_store.callback_data)
    assert cb.message.answers[-1] == vpn_settings.NO_STORE_TEXT
    assert "лучше, чем ничего" in vpn_settings.NO_STORE_TEXT
    assert _texts(cb.message.answer_markups[-1]) == ["📦 Скачать AmneziaWG (.apk)"]
    assert _all_buttons(cb.message.answer_markups[-1])[0].callback_data == "act:vpn:apk:send"


# --- новое устройство --------------------------------------------------------


async def test_new_device_pick_platform_then_get_settings_screen(_env):
    _env["servers"] = [
        _srv("jeeves", "🇳🇱 Нидерланды", [], transports=["reality", "awg"]),
        _srv("wooster", "🇺🇸 США", [], transports=["reality", "awg"]),
    ]
    cb, _, _ = await _press("act:vpn:vpn_card:n")
    assert _texts(cb.message.edit_markups[-1]) == [
        "🍎 iPhone",
        "🤖 Android",
        "💻 Компьютер",
        "⬅️ Назад",
    ]
    cb, link, _ = await _press("act:vpn:issue:~na")
    issued = link.of(vpn_protocol.ACTION_ISSUE)
    assert sorted(n for _, n in issued) == ["jeeves", "wooster"]
    assert all(a["device_label"] == "🤖 Android" and a["transport"] == "reality" for a, _ in issued)
    text, markup = _last(cb)
    assert text == "📥 <b>🤖 Android</b> — чем подключаться?"
    assert _texts(markup)[0] == "🛡 VLESS · Hiddify"


async def test_new_device_label_gets_number_when_taken():
    cb, link, _ = await _press("act:vpn:issue:~ni")  # «📱 iPhone» уже есть
    assert {a["device_label"] for a, _ in link.of(vpn_protocol.ACTION_ISSUE)} == {"📱 iPhone 2"}


async def test_new_device_failure_offers_retry():
    cb, _, _ = await _press("act:vpn:issue:~ni", link=Link(fail_issue_on={"jeeves", "wooster"}))
    text, markup = _last(cb)
    assert "Не получилось" in text
    assert _texts(markup) == ["🔁 Попробовать ещё раз", "⬅️ Назад"]


async def test_expert_choice_without_devices_opens_expert_home(_env):
    _env["servers"] = [_srv("jeeves", "🇳🇱 Нидерланды", [], transports=["reality", "awg"])]
    cb, _, _ = await _press("act:vpn:vpn_card:wx")
    text, markup = _last(cb)
    assert text.startswith("📶 <b>VPN</b>")
    flat = _texts(markup)
    # устройств нет — первой идёт «Подключить по шагам», новое устройство — в «Управлении»
    assert flat[0] == "📱 Подключить по шагам"
    assert "⚙️ Управление устройствами" in flat and "➕ Новое устройство" not in flat
    # «Управление» при пустом списке — пустой список, а не мастер
    cb, _, _ = await _press("act:vpn:vpn_card:m")
    text, markup = _last(cb)
    assert "Устройств пока нет." in text and "➕ Новое устройство" in _texts(markup)


# --- ограничения Telegram ----------------------------------------------------


async def test_callback_data_within_64_bytes():
    servers = _servers(nl=("reality", "awg"))
    device = vd.build_devices(servers)[0]
    markups = [
        vpn_settings.pick_keyboard(device.key),
        vpn_settings.awg_pick_keyboard(device.key, servers),
        vpn_settings.awg_replace_keyboard(device.key, "wooster"),
        vpn_settings.new_device_keyboard(),
        vpn_settings.create_failed_keyboard("a"),
        vpn_settings.vless_keyboard(
            page_url=PAGE,
            file_again_key=device.key,
            qr_buttons=[("📷 QR 🇳🇱", "act:vpn:issue:f_" + "x" * 8)] * 2,
        ),
        vpn_settings.awg_steps_keyboard(VpnConfig(), "act:vpn:issue:f_" + "x" * 8),
        vpn_settings.no_store_keyboard(),
        h._card_keyboard_for_device(device, servers, subscription=GUEST_FULL),
    ]
    for markup in markups:
        for b in _all_buttons(markup):
            if b.callback_data:
                assert len(b.callback_data.encode()) <= 64, b.callback_data


# --- 57.11: «Получить настройки» — одно пересылаемое сообщение ----------------


def _card_markup(page_url):
    servers = _servers()
    device = vd.build_devices(servers)[0]
    return h._card_keyboard_for_device(device, servers, subscription=GUEST_FULL, page_url=page_url)


def test_card_page_button_first_then_link_and_other_ways():
    markup = _card_markup(PAGE)
    first = _all_buttons(markup)[0]
    assert first.text == "📶 Настройки на странице" and first.url == PAGE
    texts = _texts(markup)
    get = [b for b in _all_buttons(markup) if b.text == "📥 Получить настройки"][0]
    other = [b for b in _all_buttons(markup) if b.text == "Другие способы"][0]
    assert get.callback_data.endswith(f":l{KEY}") and other.callback_data.endswith(f":g{KEY}")
    assert texts.index("📥 Получить настройки") < texts.index("Другие способы")


def test_card_without_page_keeps_old_get_settings():
    markup = _card_markup(None)
    get = [b for b in _all_buttons(markup) if b.text == "📥 Получить настройки"][0]
    assert get.callback_data.endswith(f":g{KEY}")
    assert "Другие способы" not in _texts(markup)


async def test_get_settings_sends_one_forwardable_message(monkeypatch):
    async def fake_fanout(link, action, args):
        if action == vpn_protocol.ACTION_GET_SUBSCRIPTION:
            return [{"page_url": PAGE}]
        return _servers()

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    cb, link, notifier = await _press(f"act:vpn:vpn_card:l{KEY}")
    assert len(notifier.sent_direct) == 1 and not notifier.sent_documents
    chat, text = notifier.sent_direct[0]
    assert chat == 778
    assert text == (
        f"📶 <b>Настройки VPN · {LABEL}</b>\n"
        "Откройте ссылку на телефоне, который подключаете — там всё по шагам: "
        "Hiddify, AmneziaWG и проверка, что VPN включён.\n"
        f"{PAGE}\n"
        "Ссылку можно переслать. Не делитесь ей с чужими: по ней подключается это устройство."
    )
    markup = notifier.sent_direct_markups[0]
    buttons = _all_buttons(markup)
    assert [(b.text, b.url, b.callback_data) for b in buttons] == [
        ("📶 Открыть настройки", PAGE, None)
    ]
    assert not notifier.deleted  # для пересылки: по TTL не удаляется
    assert not [c for c in link.calls if c[0] == vpn_protocol.ACTION_ISSUE]


async def test_get_settings_link_private_only_and_needs_page(monkeypatch):
    async def fake_fanout(link, action, args):
        if action == vpn_protocol.ACTION_GET_SUBSCRIPTION:
            return []
        return _servers()

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    cb, _, notifier = await _press(f"act:vpn:vpn_card:l{KEY}")
    assert not notifier.sent_direct and cb.answered[0][1].get("show_alert")
    cb, _, notifier = await _press(f"act:vpn:vpn_card:l{KEY}", chat_id=-100)
    assert not notifier.sent_direct and cb.answered[0][1].get("show_alert")


def test_help_texts_mention_page_and_vpn_check():
    from sa_home_bot.bot import vpn_facts

    joined = " ".join(vpn_facts.ANSWERS.values())
    assert "страниц" in joined and "включён ли VPN" in joined
