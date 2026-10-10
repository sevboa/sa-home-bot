"""«❓ Помощь» и «✈️ Прокси Telegram» в /vpn (этап 57.6)."""

from __future__ import annotations

import base64

import pytest

from sa_home_bot.bot import vpn_faq, vpn_nodes, vpn_proxy_screen
from sa_home_bot.bot.handlers import vpn as h
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

from .test_vpn_expert import _all_buttons, _texts
from .test_vpn_handler import GUEST_FULL, FakeCallback, FakeNodeLink, FakeNotifier

CFG = Settings(vpn=VpnConfig())
PROXY_GUEST = Subscription(
    chat_id=779,
    name="proxy-guest",
    allowed_commands=frozenset({"vpn_card@vpn", "proxy_link@vpn", "apk@vpn"}),
)
SECRET = "deadbeefsecret"


def _proxy(node, label, *, qr="UE5H"):
    return {
        "node": node,
        "label": label,
        "tg_link": f"tg://proxy?server={node}&port=443&secret={SECRET}",
        "t_me_link": f"https://t.me/proxy?server={node}&port=443&secret={SECRET}",
        "host": f"{node}.example",
        "port": 443,
        "secret": SECRET,
        "socks_host": "100.1.1.1",
        "socks_port": 1080,
        "qr_png_b64": qr,
    }


@pytest.fixture
def results(monkeypatch):
    state = {"r": [_proxy("jeeves", "🇳🇱 Нидерланды"), _proxy("wooster", "🇺🇸 США")]}

    async def fake_fanout(node_link, action, args):
        return state["r"]

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    return state


async def _press(data, sub=GUEST_FULL, chat_id=778):
    cb = FakeCallback(data, chat_id=chat_id)
    notifier = FakeNotifier()
    await h.handle_action(cb, FakeNodeLink(), notifier, CFG, sub, PendingVpnSecrets())
    return cb, notifier


# --- справка -----------------------------------------------------------------


async def test_faq_list_has_all_questions_and_no_report_button():
    cb, _ = await _press("act:vpn:apk")
    markup = cb.message.edit_markups[-1]
    assert _texts(markup) == [
        "Что такое Hiddify?",
        "Чем отличается AmneziaWG?",
        "Не подключается",
        "Как подключить ещё одно устройство",
        "Магазин недоступен?",
        "🤵 Спросить Альфреда",
        "⬅️ Назад",
    ]
    assert "Сообщить" not in " ".join(_texts(markup))


@pytest.mark.parametrize("code", ["h", "a", "c", "d", "s", "x"])
async def test_every_answer_has_back_to_questions_and_short_callbacks(code):
    cb, _ = await _press(f"act:vpn:apk:q{code}")
    markup = cb.message.edit_markups[-1]
    assert _texts(markup)[-1] == "⬅️ К вопросам"
    assert _all_buttons(markup)[-1].callback_data == "act:vpn:apk"
    for button in _all_buttons(markup):
        assert button.url or len(button.callback_data.encode()) <= 64
    assert len(cb.message.edits[-1].splitlines()) <= 5


async def test_hiddify_answer_has_text_and_hiddify_stores_only():
    cb, _ = await _press("act:vpn:apk:qh")
    assert "«VLESS · Hiddify»" in cb.message.edits[-1] and "одно и то же" in cb.message.edits[-1]
    urls = [b.url for b in _all_buttons(cb.message.edit_markups[-1]) if b.url]
    assert CFG.vpn.hiddify_ios_app_store_url in urls and CFG.vpn.hiddify_google_play_url in urls
    assert CFG.vpn.ios_app_store_url not in urls


async def test_amneziawg_answer_links_only_amneziavpn():
    cb, _ = await _press("act:vpn:apk:qa")
    text = cb.message.edits[-1]
    assert "AmneziaVPN" in text and "VLESS · Hiddify" in text
    urls = [b.url for b in _all_buttons(cb.message.edit_markups[-1]) if b.url]
    assert urls == [
        CFG.vpn.amneziavpn_ios_app_store_url,
        CFG.vpn.amneziavpn_google_play_url,
        CFG.vpn.official_download_url,
    ]
    assert CFG.vpn.ios_app_store_url not in urls and CFG.vpn.google_play_url not in urls


async def test_device_answer_has_wizard_button_only_with_issue_right():
    cb, _ = await _press("act:vpn:apk:qd")
    assert "📱 Подключить по шагам" in _texts(cb.message.edit_markups[-1])
    cb, _ = await _press("act:vpn:apk:qd", sub=PROXY_GUEST)
    assert "📱 Подключить по шагам" not in _texts(cb.message.edit_markups[-1])


async def test_store_answer_has_apk_and_hiddify_site():
    cb, _ = await _press("act:vpn:apk:qs")
    markup = cb.message.edit_markups[-1]
    assert "act:vpn:apk:send" in [b.callback_data for b in _all_buttons(markup)]
    urls = [b.url for b in _all_buttons(markup) if b.url]
    assert CFG.vpn.hiddify_site_url in urls and CFG.vpn.hiddify_releases_url in urls
    assert "лучше, чем ничего" in cb.message.edits[-1]


async def test_ask_alfred_points_to_command():
    cb, _ = await _press("act:vpn:apk:qx")
    assert "/alfred" in cb.message.edits[-1]


async def test_unknown_question_falls_back_to_list():
    cb, _ = await _press("act:vpn:apk:qzzz")
    assert cb.message.edits[-1] == vpn_faq.LIST_TEXT


async def test_old_apps_screen_is_gone_from_home_keyboards():
    flat = " ".join(
        b.text
        for row in h._card_keyboard(
            [], subscription=GUEST_FULL, self_serve_nodes=[]
        ).inline_keyboard
        for b in row
    )
    assert "Приложение" not in flat and "❓ Помощь" in flat


# --- прокси ------------------------------------------------------------------


async def test_proxy_screen_url_buttons_per_country(results):
    cb, _ = await _press(f"act:vpn:{vpn_protocol.ACTION_PROXY_LINK}", sub=PROXY_GUEST, chat_id=779)
    text = cb.message.edits[-1]
    assert "Telegram без VPN" in text and SECRET not in text
    markup = cb.message.edit_markups[-1]
    buttons = _all_buttons(markup)
    connect = [b for b in buttons if b.text.endswith("Подключить")]
    assert [b.text for b in connect] == ["🇳🇱 Подключить", "🇺🇸 Подключить"]
    assert all(b.url.startswith("https://t.me/proxy?") for b in connect)
    assert [b.text for b in buttons if "QR" in b.text] == ["📷 QR 🇳🇱", "📷 QR 🇺🇸"]
    assert "⚙️ Секрет и порт" not in _texts(markup)  # гостю — нет
    for b in buttons:
        assert b.url or len(b.callback_data.encode()) <= 64


async def test_proxy_screen_admin_gets_secret_button(results):
    cb, _ = await _press(f"act:vpn:{vpn_protocol.ACTION_PROXY_LINK}")
    assert "⚙️ Секрет и порт" in _texts(cb.message.edit_markups[-1])


async def test_proxy_dead_node_has_no_buttons(results):
    results["r"] = [_proxy("wooster", "🇺🇸 США")]
    cb, _ = await _press(f"act:vpn:{vpn_protocol.ACTION_PROXY_LINK}")
    texts = _texts(cb.message.edit_markups[-1])
    assert "🇺🇸 Подключить" in texts and "🇳🇱 Подключить" not in texts
    assert "📷 QR" in texts  # одна страна — без флага


async def test_proxy_secret_screen_admin_only(results):
    cb, _ = await _press(f"act:vpn:proxy_link:{vpn_proxy_screen.VALUE_SECRET}", sub=PROXY_GUEST)
    assert cb.message.answers == []  # секрета гостю не показано
    assert cb.answered and cb.answered[-1][1].get("show_alert")
    cb, _ = await _press(f"act:vpn:proxy_link:{vpn_proxy_screen.VALUE_SECRET}")
    assert len(cb.message.answers) == 2
    assert SECRET in cb.message.answers[0] and "100.1.1.1" in cb.message.answers[0]


async def test_proxy_qr_sends_photo_for_country():
    class Link(FakeNodeLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            self.dsts.append(dst)
            return _proxy("wooster", "🇺🇸 США", qr=base64.b64encode(b"png").decode())

    cb = FakeCallback("act:vpn:proxy_link:qr:wooster", chat_id=779)
    notifier = FakeNotifier()
    await h.handle_action(cb, Link(), notifier, CFG, PROXY_GUEST, PendingVpnSecrets())
    assert notifier.sent_photos[0][1] == b"png" and "🇺🇸" in notifier.sent_photos[0][2]
