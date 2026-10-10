"""«⚠️ Сообщить о проблеме» / «💬 Ответить» в /vpn (этап 57.6b)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_help, vpn_nodes, vpn_report
from sa_home_bot.bot.handlers import vpn as h
from sa_home_bot.bot.middlewares import CallbackAuthorizationMiddleware
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

from .test_vpn_expert import KEY, LABEL, _conn, _srv, _texts
from .test_vpn_handler import GUEST_FULL, FakeCallback, FakeNodeLink, FakeNotifier

OWNER = Subscription(chat_id=1, name="owner", allowed_commands=frozenset({"*"}))
VIEW_ONLY = Subscription(chat_id=779, name="v", allowed_commands=frozenset({"usage@vpn"}))
NO_VPN = Subscription(chat_id=780, name="p", allowed_commands=frozenset({"proxy_link@vpn"}))


class User:
    id = 778
    full_name = "Алексей"
    username = "alex"


class Sent:
    def __init__(self, message_id):
        self.message_id = message_id


def _servers(*, hs="2026-10-10T14:02:00+00:00", check=True):
    nl = _srv(
        "jeeves",
        "🇳🇱 Нидерланды",
        [
            {
                "device_label": LABEL,
                "used_bytes": 0,
                "connections": [_conn("reality", hs)],
            }
        ],
        check=[{"transport": "reality", "status": "ok"}] if check else [],
    )
    us = _srv(
        "wooster",
        "🇺🇸 США",
        [{"device_label": LABEL, "used_bytes": 0, "connections": [_conn("reality")]}],
        check=[{"transport": "reality", "status": "partial"}] if check else [],
    )
    return [nl, us]


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn_nodes, "KNOWN_SERVERS_PATH", tmp_path / "known.json")
    vpn_help.reset_throttle()
    vpn_report.reset_pending()
    state = {"servers": _servers()}

    async def fake_fanout(node_link, action, args):
        return state["servers"]

    monkeypatch.setattr(vpn_nodes, "fanout", fake_fanout)
    return state


def _book():
    return SubscriptionBook([OWNER, GUEST_FULL, VIEW_ONLY])


async def _press(data, sub=GUEST_FULL, chat_id=778, book=None, notifier=None, user=User, mid=500):
    notifier = notifier or FakeNotifier()
    cb = FakeCallback(data, chat_id=chat_id)
    cb.from_user = user
    asked = []

    async def answer(text, reply_markup=None, **kw):
        asked.append((text, reply_markup))
        return Sent(mid)

    cb.message.answer = answer
    cb.asked = asked
    await h.handle_action(
        cb,
        FakeNodeLink(),
        notifier,
        Settings(vpn=VpnConfig()),
        sub,
        PendingVpnSecrets(),
        book if book is not None else _book(),
    )
    return cb, notifier


def _msg(text, reply_to, *, chat_id=778, user_id=778):
    sent = []

    async def answer(text, **kw):
        sent.append(text)

    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=user_id, full_name="Алексей", username="alex"),
        reply_to_message=SimpleNamespace(message_id=reply_to),
        answer=answer,
        sent=sent,
    )


async def _reply(message, notifier, sub=GUEST_FULL, book=None):
    flt = await h.VpnReplyFilter()(message)
    if not flt:
        return False
    await h.on_vpn_reply(
        message,
        flt["vpn_reply"],
        FakeNodeLink(),
        notifier,
        book if book is not None else _book(),
        None,
        sub,
    )
    return True


# --- экраны ------------------------------------------------------------------


async def test_menu_from_home_device_and_faq_have_right_back():
    cb, _ = await _press("act:vpn:report:m")
    assert cb.message.edits[-1] == "Что случилось?"
    markup = cb.message.edit_markups[-1]
    assert _texts(markup) == [
        "🔌 Не подключается",
        "🐢 Работает медленно",
        "🌐 Не открываются сайты",
        "✍️ Другое — опишу словами",
        "⬅️ Назад",
    ]
    assert markup.inline_keyboard[-1][0].callback_data == "act:vpn:vpn_card"
    cb, _ = await _press(f"act:vpn:report:m{KEY}")
    last = cb.message.edit_markups[-1].inline_keyboard
    assert last[0][0].callback_data == f"act:vpn:report:c{KEY}"
    assert last[-1][0].callback_data == f"act:vpn:vpn_card:d{KEY}"
    cb, _ = await _press("act:vpn:report:f")
    assert cb.message.edit_markups[-1].inline_keyboard[-1][0].callback_data == "act:vpn:apk"


def test_every_report_callback_fits_64_bytes():
    key = "f" * 8
    cbs = [vpn_report.report_cb(c, key) for c in "mcswof"]
    cbs += [vpn_report.reply_cb(-1001234567890123), vpn_report.reply_cb(778)]
    kb = vpn_report.menu_keyboard(key, back_cb=f"act:vpn:vpn_card:d{key}")
    cbs += [b.callback_data for row in kb.inline_keyboard for b in row]
    cbs += [
        b.callback_data
        for row in vpn_report.owner_keyboard(
            -1001234567890123, "act:vpn:peers:g-1001234567890123"
        ).inline_keyboard
        for b in row
    ]
    assert all(len(c.encode()) <= 64 for c in cbs)


# --- причины кнопкой и уведомление ------------------------------------------


async def test_reason_button_sends_full_owner_notification():
    cb, notifier = await _press(f"act:vpn:report:c{KEY}")
    assert cb.message.edits[-1] == "✅ Передал владельцу. Ответ придёт сюда."
    (chat, text), markup = notifier.sent_direct[0], notifier.sent_direct_markups[0]
    assert chat == 1
    lines = text.split("\n")
    assert lines[0] == "⚠️ VPN: проблема у <b>Алексей</b> (@alex)"
    assert lines[1] == "Причина: не подключается"
    assert lines[2] == f"Устройство: {LABEL} · VLESS · Hiddify"
    assert lines[3].startswith("🇳🇱 на связи 10.10 ") and lines[3].endswith("🇺🇸 ещё не подключалось")
    assert lines[4] == "Проверки: 🇳🇱 🟢 · 🇺🇸 🟠"
    assert lines[5] == "Трафик: 🇳🇱 13.0 / 100 ГБ · 🇺🇸 13.0 / 100 ГБ"
    assert [b.text for b in markup.inline_keyboard[0]] == ["💬 Ответить", "👤 Карточка гостя"]
    assert markup.inline_keyboard[0][0].callback_data == "act:vpn:reply:778"


async def test_no_device_lists_all_devices_and_down_node_is_named(_env, monkeypatch):
    _env["servers"] = _servers(check=False)[:1]
    vpn_nodes.remember_servers([{"node": "wooster", "label": "🇺🇸 США"}])
    cb, notifier = await _press("act:vpn:report:s")
    text = notifier.sent_direct[0][1]
    assert "Причина: работает медленно" in text
    assert "Устройства:" not in text  # устройство одно — формат одного
    assert f"Устройство: {LABEL} · VLESS · Hiddify" in text
    assert "🇺🇸 нода не ответила" in text and "Проверки" not in text


async def test_several_devices_shown_briefly(_env):
    servers = _servers()
    servers[0]["device_usage"].append(
        {"device_label": "💻 Компьютер", "used_bytes": 0, "connections": [_conn("reality")]}
    )
    _env["servers"] = servers
    _, notifier = await _press("act:vpn:report:w")
    text = notifier.sent_direct[0][1]
    assert "Причина: не открываются сайты" in text
    assert "Устройства:\n" in text and f"• {LABEL} — 🇳🇱 на связи" in text
    assert "• 💻 Компьютер — 🇳🇱 ещё не подключалось" in text


# --- «Другое» и reply --------------------------------------------------------


async def test_other_asks_with_force_reply_and_own_reply_reaches_owner():
    cb, notifier = await _press(f"act:vpn:report:o{KEY}", mid=500)
    text, markup = cb.asked[0]
    assert text == "Опишите проблему одним сообщением — ответьте на это сообщение."
    assert type(markup).__name__ == "ForceReply"
    assert not notifier.sent_direct
    msg = _msg("Не грузится <YouTube>", 500)
    notifier2 = FakeNotifier()
    assert await _reply(msg, notifier2)
    assert msg.sent == ["✅ Передал владельцу. Ответ придёт сюда."]
    sent = notifier2.sent_direct[0][1]
    assert "Причина: Не грузится &lt;YouTube&gt;" in sent
    assert f"Устройство: {LABEL}" in sent
    # одно ожидание — одна заявка
    assert vpn_report.lookup(778, 500) is None


async def test_foreign_or_unrelated_reply_is_ignored():
    await _press("act:vpn:report:o", mid=500)
    notifier = FakeNotifier()
    assert not await _reply(_msg("привет", 500, user_id=999), notifier)  # чужой человек
    assert not await _reply(_msg("привет", 501), notifier)  # reply на другое сообщение
    assert not await _reply(_msg("/vpn", 500), notifier)  # команда
    assert not notifier.sent_direct


async def test_expired_reply_is_refused(monkeypatch):
    await _press("act:vpn:report:o", mid=500)
    real = vpn_report.time.monotonic()
    monkeypatch.setattr(vpn_report.time, "monotonic", lambda: real + vpn_report.REPLY_TTL_S + 1)
    msg, notifier = _msg("поздно", 500), FakeNotifier()
    assert await _reply(msg, notifier)
    assert msg.sent == [vpn_report.EXPIRED_TEXT] and not notifier.sent_direct
    assert vpn_report.lookup(778, 500) is None


# --- троттлинг общий ---------------------------------------------------------


async def test_throttle_is_shared_with_wizard_help(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(vpn_help.time, "monotonic", lambda: clock[0])
    _, n1 = await _press("act:vpn:vpn_card:whci" + KEY)  # мастер: «Позвать на помощь»
    assert len(n1.sent_direct) == 1
    cb, n2 = await _press(f"act:vpn:report:c{KEY}")
    assert not n2.sent_direct and cb.answered[0][0] == ("Уже передал, ждите ответа.",)
    cb, _ = await _press("act:vpn:report:o")
    assert not cb.asked and cb.answered[0][0] == ("Уже передал, ждите ответа.",)
    clock[0] += 601
    _, n3 = await _press(f"act:vpn:report:c{KEY}")
    assert len(n3.sent_direct) == 1
    _, n4 = await _press("act:vpn:vpn_card:whci" + KEY)  # и наоборот
    assert not n4.sent_direct


async def test_reply_while_throttled_says_so():
    await _press("act:vpn:report:o", mid=500)
    await _press(f"act:vpn:report:c{KEY}")  # заявка ушла кнопкой, пока ждали описание
    msg = _msg("ещё", 500)
    assert await _reply(msg, FakeNotifier())
    assert msg.sent == ["Уже передал, ждите ответа."]


# --- «💬 Ответить» -----------------------------------------------------------


async def test_owner_reply_button_and_forwarding():
    cb, _ = await _press(
        "act:vpn:reply:778",
        sub=OWNER,
        chat_id=1,
        user=SimpleNamespace(id=1, full_name="Босс", username=None),
        mid=900,
    )
    text, markup = cb.asked[0]
    assert text == "Ответ для guest-full — ответьте на это сообщение." or "Ответ для" in text
    assert type(markup).__name__ == "ForceReply"
    msg = _msg("Попробуйте <другую> страну", 900, chat_id=1, user_id=1)
    notifier = FakeNotifier()
    assert await _reply(msg, notifier, sub=OWNER)
    assert notifier.sent_direct == [(778, "💬 Ответ владельца: Попробуйте &lt;другую&gt; страну")]
    assert msg.sent == ["✅ Отправил."]


async def test_reply_button_is_admin_only():
    cb, notifier = await _press(
        "act:vpn:reply:778",
        sub=GUEST_FULL.__class__(chat_id=778, name="g", allowed_commands=frozenset({"usage@vpn"})),
    )
    assert cb.answered[0][1] == {"show_alert": True} and not cb.asked and not notifier.sent_direct
    # не админ не получает ожидание даже подделав id
    assert vpn_report.lookup(778, 500) is None


# --- права -------------------------------------------------------------------


def test_can_report_rights():
    assert vpn_report.can_report(VIEW_ONLY) and vpn_report.can_report(OWNER)
    assert not vpn_report.can_report(NO_VPN) and not vpn_report.can_report(None)
    explicit = Subscription(chat_id=5, name="e", allowed_commands=frozenset({"report@vpn"}))
    assert vpn_report.can_report(explicit)
    assert vpn_report.can_reply(OWNER) and not vpn_report.can_reply(VIEW_ONLY)


@pytest.mark.parametrize(
    ("data", "sub", "allowed"),
    [
        ("act:vpn:report:m", VIEW_ONLY, True),
        ("act:vpn:report:m", NO_VPN, False),
        ("act:vpn:reply:778", VIEW_ONLY, False),
        ("act:vpn:reply:778", OWNER, True),
    ],
)
async def test_middleware_rights(data, sub, allowed):
    book = SubscriptionBook([sub])
    mw = CallbackAuthorizationMiddleware(book)
    answers, called = [], []

    async def answer(*a, **kw):
        answers.append(a)

    event = SimpleNamespace(
        data=data, message=SimpleNamespace(chat=SimpleNamespace(id=sub.chat_id)), answer=answer
    )

    async def handler(ev, d):
        called.append(1)

    await mw(handler, event, {})
    assert bool(called) is allowed


async def test_probe_chat_zero_is_never_reported():
    notifier = FakeNotifier()
    out = await h._submit_report(
        FakeNodeLink(),
        notifier,
        _book(),
        None,
        chat_id=0,
        user=None,
        subscription=OWNER,
        reason="x",
    )
    assert out == "nobody" and not notifier.sent_direct
    cb, n = await _press("act:vpn:report:c", chat_id=0)
    assert not n.sent_direct


def test_vpn_toggles_do_not_include_report():
    from sa_home_bot.bot import vpn_admin_view

    assert not any("report@vpn" in t.rights for t in vpn_admin_view.VPN_TOGGLES)
    assert vd  # импорт нужен модулю для KEY
    assert vpn_protocol.SERVICE_NAME == "vpn"
