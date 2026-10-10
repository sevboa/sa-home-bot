"""bot/handlers/vpn.py + bot/vpn_wizard.py: пошаговая настройка (57.3a)."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_help, vpn_nodes
from sa_home_bot.bot import vpn_wizard as w
from sa_home_bot.bot.handlers import vpn as h
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

from .test_vpn_expert import _all_buttons, _conn, _srv, _texts
from .test_vpn_handler import (
    ADMIN,
    GUEST_FULL,
    FakeCallback,
    FakeNodeLink,
    FakeNotifier,
)

OWNER = Subscription(chat_id=1, name="owner", allowed_commands=frozenset({"*"}))


class User:
    full_name = "Алексей"
    username = "alex"


class Link(FakeNodeLink):
    def __init__(self, *, fail_nodes=(), fail_all=False):
        super().__init__()
        self.fail_nodes = set(fail_nodes)
        self.fail_all = fail_all

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        self.dsts.append(dst)
        if action == vpn_protocol.ACTION_ISSUE:
            if self.fail_all or dst.node in self.fail_nodes:
                raise ProtoError("bad_request", "нет")
            return {"device_label": args["device_label"], "transport": "reality"}
        if action == vpn_protocol.ACTION_GET_VLESS:
            return {"config_text": '{"singbox": 1}', "location": "🇳🇱 Нидерланды"}
        if action == vpn_protocol.ACTION_GET_SUBSCRIPTION:
            return {"page_url": "https://1.2.3.4:8444/s/tok"}
        return {}


def _empty():
    return [
        _srv("jeeves", "🇳🇱 Нидерланды", [], transports=["reality"]),
        _srv("wooster", "🇺🇸 США", [], transports=["reality", "awg"]),
    ]


def _with(label, *, hs=None, nodes=("jeeves", "wooster")):
    srv = _empty()
    for s in srv:
        if s["node"] in nodes:
            s["device_usage"] = [
                {
                    "device_label": label,
                    "used_bytes": 0,
                    "connections": [_conn("reality", hs)],
                }
            ]
    return srv


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn_nodes, "KNOWN_SERVERS_PATH", tmp_path / "known.json")
    vpn_help.reset_throttle()
    state = {"servers": _empty()}

    async def fake_fanout(node_link, action, args):
        if action == vpn_protocol.ACTION_GET_SUBSCRIPTION:
            return [{"page_url": "https://1.2.3.4:8444/s/tok"}]
        return state["servers"]

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    return state


def _cfg(**kw):
    return Settings(vpn=VpnConfig(**kw))


async def _press(data, sub=GUEST_FULL, link=None, chat_id=778, cfg=None, book=None, notifier=None):
    link = link or Link()
    notifier = notifier or FakeNotifier()
    cb = FakeCallback(data, chat_id=chat_id)
    cb.from_user = User()
    await h.handle_action(cb, link, notifier, cfg or _cfg(), sub, PendingVpnSecrets(), book)
    return cb, link, notifier


def _last(cb):
    return cb.message.edits[-1], cb.message.edit_markups[-1]


# --- первый экран ------------------------------------------------------------


async def test_no_devices_shows_wizard_intro():
    cb, _, _ = await _press("act:vpn:vpn_card")
    text, markup = _last(cb)
    assert text == (
        "📶 <b>VPN</b>\n\nПодключим ваш телефон или компьютер?\n"
        "Проведу по шагам — займёт пару минут."
    )
    assert _texts(markup) == ["📱 Подключить по шагам", "⚙️ Я разберусь сам"]


async def test_expert_choice_shows_old_card_and_is_not_remembered():
    cb, _, _ = await _press("act:vpn:vpn_card:wx")
    text, markup = _last(cb)
    assert "Подключим" not in text and "Устройств нет" not in "".join(_texts(markup))
    assert "⚙️ Управление устройствами" in _texts(markup) or "📱 Приложение" in _texts(markup)
    cb, _, _ = await _press("act:vpn:vpn_card")
    assert "Подключим ваш телефон" in _last(cb)[0]


async def test_no_wizard_without_issue_right_or_reality_or_in_group(_env):
    view = Subscription(
        chat_id=5, name="v", allowed_commands=frozenset({"usage@vpn", "vpn_card@vpn"})
    )
    cb, _, _ = await _press("act:vpn:vpn_card", sub=view, chat_id=5)
    assert "Подключим" not in _last(cb)[0]
    cb, _, _ = await _press("act:vpn:vpn_card", chat_id=-100)
    assert "Подключим" not in _last(cb)[0]
    _env["servers"] = [_srv("jeeves", "🇳🇱", [], transports=["awg"])]
    cb, _, _ = await _press("act:vpn:vpn_card")
    assert "Подключим" not in _last(cb)[0]


async def test_wizard_button_in_list_not_on_home_when_devices_exist(_env):
    _env["servers"] = _with("📱 iPhone")
    cb, _, _ = await _press("act:vpn:vpn_card")
    assert "📱 Подключить по шагам" not in _texts(cb.message.edit_markups[-1])
    cb, _, _ = await _press("act:vpn:vpn_card:m")
    assert "📱 Подключить по шагам" in _texts(cb.message.edit_markups[-1])


async def test_pick_screen():
    cb, _, _ = await _press("act:vpn:vpn_card:wp")
    text, markup = _last(cb)
    assert text == "Какое у вас устройство?"
    assert _texts(markup) == ["🍎 iPhone", "🤖 Android", "💻 Компьютер", "⬅️ Назад"]


# --- создание устройства ------------------------------------------------------


async def test_create_issues_vless_in_every_country_and_shows_page_step():
    cb, link, _ = await _press("act:vpn:issue:~wi", cfg=_cfg(wizard_settings_file=False))
    issues = [
        (d.node, a)
        for (act, a), d in zip(link.calls, link.dsts, strict=True)
        if act == vpn_protocol.ACTION_ISSUE
    ]
    assert sorted(n for n, _ in issues) == ["jeeves", "wooster"]
    assert all(
        a == {"chat_id": 778, "device_label": "📱 iPhone", "transport": "reality"}
        for _, a in issues
    )
    text, markup = _last(cb)
    assert text == (
        "📶 <b>Настройки VPN · 📱 iPhone</b>\n"
        "Откройте ссылку на устройстве, которое подключаете (телефон или компьютер) — там всё "
        "по шагам: AmneziaVPN, Hiddify и проверка, что VPN включён.\n"
        "https://1.2.3.4:8444/s/tok\n"
        "Ссылку можно переслать. Не делитесь ей с чужими: по ней подключается это устройство."
    )
    assert "⋮" not in text
    page = _all_buttons(markup)[0]
    assert page.text == "📶 Открыть настройки" and page.url == "https://1.2.3.4:8444/s/tok"
    assert _texts(markup)[1:] == ["✅ Готово", "🙋 Не получается"]


@pytest.mark.parametrize(("code", "label"), [("a", "🤖 Android"), ("c", "💻 Компьютер")])
async def test_platforms(code, label):
    cb, link, _ = await _press(f"act:vpn:issue:~w{code}", cfg=_cfg(wizard_settings_file=False))
    assert link.calls[0][1]["device_label"] == label
    first = _all_buttons(_last(cb)[1])[0]
    assert first.text == "📶 Открыть настройки" and "/s/tok" in first.url


def test_device_names_get_numbers():
    ip = w.PLATFORMS["i"]
    assert w.next_label(ip, set()) == "📱 iPhone"
    assert w.next_label(ip, {"📱 iPhone"}) == "📱 iPhone 2"
    assert w.next_label(ip, {"📱 iPhone", "📱 iPhone 2"}) == "📱 iPhone 3"
    assert w.next_label(w.PLATFORMS["a"], {"📱 iPhone"}) == "🤖 Android"


async def test_second_android_is_numbered(_env):
    _env["servers"] = _with("🤖 Android")
    _, link, _ = await _press("act:vpn:issue:~wa")
    assert link.calls[0][1]["device_label"] == "🤖 Android 2"


async def test_partial_node_failure_continues():
    cb, link, _ = await _press(
        "act:vpn:issue:~wi", link=Link(fail_nodes={"wooster"}), cfg=_cfg(wizard_settings_file=False)
    )
    assert _last(cb)[0].startswith("📶 <b>Настройки VPN")


async def test_all_nodes_failing_says_so_and_offers_help():
    cb, _, _ = await _press("act:vpn:issue:~wi", link=Link(fail_all=True))
    text, markup = _last(cb)
    assert "серверы сейчас не отвечают" in text and "позовите на помощь" in text
    assert _texts(markup) == ["🔁 Попробовать ещё раз", "🙋 Позвать на помощь", "⬅️ Назад"]


async def test_create_refused_in_group():
    cb, link, _ = await _press("act:vpn:issue:~wi", chat_id=-100)
    assert cb.answered[0][1].get("show_alert")
    assert not [c for c in link.calls if c[0] == vpn_protocol.ACTION_ISSUE]


# --- шаги и нумерация ---------------------------------------------------------

LABEL = "📱 iPhone"
KEY = vd.device_key(LABEL)


async def test_no_file_step_by_default_page_first(_env):
    _env["servers"] = _with(LABEL)
    # старая кнопка шага «файл» ведёт на страницу: файла в Telegram больше нет
    cb, _, notifier = await _press(f"act:vpn:vpn_card:wbi{KEY}")
    assert cb.message.edits[-1].startswith("📶 <b>Настройки VPN")
    assert not notifier.sent_documents


async def test_no_file_step_when_vless_already_worked(_env):
    other = _with("🤖 Android", hs="2026-10-01T10:00:00+00:00")
    for s, mine in zip(other, _with(LABEL), strict=True):
        s["device_usage"] += mine["device_usage"]
    _env["servers"] = other
    cb, _, _ = await _press(f"act:vpn:vpn_card:wai{KEY}")
    text, markup = _last(cb)
    assert text.startswith("📶 <b>Настройки VPN") and "Шаг" not in text  # единственный шаг
    ready = [b for b in _all_buttons(markup) if b.text == "✅ Готово"][0]
    assert ready.callback_data.endswith(f":wdi{KEY}")  # сразу проверка


async def test_no_file_step_when_flag_off(_env):
    _env["servers"] = _with(LABEL)
    cb, _, _ = await _press(f"act:vpn:vpn_card:wai{KEY}", cfg=_cfg(wizard_settings_file=False))
    assert cb.message.edits[-1].startswith("📶 <b>Настройки VPN")


async def test_own_handshake_still_page_step(_env):
    _env["servers"] = _with(LABEL, hs="2026-10-10T10:00:00+00:00")
    cb, _, _ = await _press(f"act:vpn:vpn_card:wai{KEY}")
    assert cb.message.edits[-1].startswith("📶 <b>Настройки VPN")


async def test_file_step_never_in_group(_env):
    _env["servers"] = _with(LABEL)
    _, _, notifier = await _press(f"act:vpn:vpn_card:wbi{KEY}", chat_id=-100)
    assert not notifier.sent_documents


async def test_connect_step_has_subscription_button(_env):
    _env["servers"] = _with(LABEL)
    cb, _, _ = await _press(f"act:vpn:vpn_card:wci{KEY}")
    connect = _all_buttons(_last(cb)[1])[0]
    assert connect.text == "📶 Открыть настройки" and connect.url == "https://1.2.3.4:8444/s/tok"


async def test_check_and_done_screens(_env):
    _env["servers"] = _with(LABEL)
    cb, _, _ = await _press(f"act:vpn:vpn_card:wdi{KEY}")
    text, markup = _last(cb)
    assert text == "Нажмите большую круглую кнопку в середине Hiddify.\nЗаработало?"
    assert _texts(markup) == ["✅ Да!", "❌ Нет"]
    cb, _, _ = await _press("act:vpn:vpn_card:wy")
    assert _last(cb)[0] == "🎉 Готово! Теперь VPN включается этой круглой кнопкой."
    assert _texts(_last(cb)[1]) == ["📶 На главную"]


async def test_fail_screen_and_other_country(_env):
    cb, _, _ = await _press(f"act:vpn:vpn_card:wfci{KEY}")
    text, markup = _last(cb)
    assert text == "Ничего страшного."
    assert _texts(markup) == [
        "🔁 Попробовать другую страну",
        "🙋 Позвать на помощь",
        "⬅️ Назад к шагу",
    ]
    assert not any("Спросить Альфреда" in t for t in _texts(markup))
    back = _all_buttons(markup)[-1].callback_data
    assert back.endswith(f":wci{KEY}")
    cb, _, _ = await _press(f"act:vpn:vpn_card:woci{KEY}")
    assert _last(cb)[0] == "В Hiddify нажмите на название подключения и выберите другую страну."


# --- помощь ------------------------------------------------------------------


def _book():
    return SubscriptionBook([OWNER, GUEST_FULL])


async def test_help_notifies_owner_with_context_and_card_button(_env):
    _env["servers"] = _with(LABEL)
    cb, _, notifier = await _press(f"act:vpn:vpn_card:whci{KEY}", book=_book())
    assert len(notifier.sent_direct) == 1
    chat, text = notifier.sent_direct[0]
    assert chat == 1
    assert text.startswith(
        "⚠️ VPN: проблема у <b>Алексей</b> (@alex)\n"
        "Причина: Застрял(а) на шаге 1 из 1 (iPhone)\n"
        f"Устройство: {LABEL} · VLESS · Hiddify\n"
    )
    reply_btn, card_btn = notifier.sent_direct_markups[0].inline_keyboard[0]
    assert reply_btn.callback_data == "act:vpn:reply:778"
    assert card_btn.callback_data == "act:vpn:peers:g778"
    assert _last(cb)[0] == "✅ Передал владельцу. Ответ придёт сюда."


async def test_help_throttled_per_person(_env, monkeypatch):
    _env["servers"] = _with(LABEL)
    clock = [1000.0]
    monkeypatch.setattr(vpn_help.time, "monotonic", lambda: clock[0])
    book = _book()
    _, _, n1 = await _press(f"act:vpn:vpn_card:whci{KEY}", book=book)
    cb, _, n2 = await _press(f"act:vpn:vpn_card:whci{KEY}", book=book)
    assert len(n1.sent_direct) == 1 and not n2.sent_direct
    assert cb.answered[0][0] == ("Уже передал, ждите ответа.",)
    other, _, n3 = await _press(
        f"act:vpn:vpn_card:whci{KEY}", book=book, chat_id=779, sub=GUEST_FULL
    )
    assert len(n3.sent_direct) == 1  # другой человек
    clock[0] += 601
    _, _, n4 = await _press(f"act:vpn:vpn_card:whci{KEY}", book=book)
    assert len(n4.sent_direct) == 1


async def test_help_when_device_not_created(_env):
    _, _, notifier = await _press("act:vpn:vpn_card:whni", book=_book())
    assert "Причина: Не удалось подготовить подключение (iPhone)" in notifier.sent_direct[0][1]


async def test_help_on_check_step_and_without_book(_env):
    _env["servers"] = _with(LABEL)
    _, _, notifier = await _press(f"act:vpn:vpn_card:whdi{KEY}", book=_book())
    assert "на проверке" in notifier.sent_direct[0][1]
    cb, _, n = await _press(f"act:vpn:vpn_card:whdi{KEY}", book=None)
    assert not n.sent_direct and "некому передать" in _last(cb)[0]


# --- callback_data -----------------------------------------------------------


def test_every_wizard_callback_fits_64_bytes():
    key = "f" * 8
    cfg = VpnConfig()
    markups = [w.intro_keyboard(), w.pick_keyboard(), w.done_keyboard()]
    for p in w.PLATFORMS:
        platform = w.PLATFORMS[p]
        markups.append(w.create_failed_keyboard(p))
        for step in (*w.STEP_ORDER, w.CHECK, w.NOT_CREATED):
            markups.append(w.fail_keyboard(step, p, key))
            markups.append(w.back_to_step_keyboard(step, p, key, help_button=True))
            markups.append(w.unavailable_keyboard(step, p, key))
        for step in (*w.STEP_ORDER, w.CHECK):
            markups.append(
                w.step_keyboard(step, platform, key, cfg, needs_file=True, page_url="https://x/s/t")
            )
    for markup in markups:
        for button in _all_buttons(markup):
            if button.callback_data:
                assert len(button.callback_data.encode()) <= 64, button.callback_data
    assert ADMIN  # импорт используется
