"""tool_vpn на модели устройств (этап 57.8): статус, «подключить» (мастер),
«дать настройки», «сообщить о проблеме»; права, секреты, chat_id=0."""

from __future__ import annotations

import pytest

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot import vpn_agent, vpn_facts, vpn_help, vpn_nodes
from sa_home_bot.config import GuestSubscriptionConfig, Settings, SubscriptionConfig
from sa_home_bot.proto.messages import Address
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

ADMIN_CHAT = 1
GUEST_CHAT = 555
SECRETS = ("vless://", "PrivateKey", "uuid-secret")

GUEST = Subscription(
    chat_id=GUEST_CHAT,
    name="guest",
    allowed_commands=frozenset({"usage@vpn", "issue@vpn", "vpn_card@vpn"}),
)
ADMIN = Subscription(chat_id=ADMIN_CHAT, name="admin", allowed_commands=frozenset({"*"}))

_SERVERS = [
    {
        "node": "jeeves",
        "label": "🇳🇱 Нидерланды",
        "transports": ["reality", "awg"],
        "limit_bytes": 100_000_000_000,
        "used_bytes": 9_100_000_000,
        "remaining_bytes": 90_900_000_000,
        "device_usage": [
            {
                "device_label": "iPhone",
                "used_bytes": 9_100_000_000,
                "connections": [
                    {
                        "transport": "reality",
                        "status": "active",
                        "last_handshake_at": "2026-10-10T09:02:00+00:00",
                        "created_at": "2026-10-01T09:02:00+00:00",
                        "used_bytes": 9_100_000_000,
                    }
                ],
            }
        ],
        "check": [{"transport": "reality", "status": "ok"}],
    },
    {
        "node": "wooster",
        "label": "🇺🇸 США",
        "transports": ["reality"],
        "limit_bytes": 100_000_000_000,
        "used_bytes": 0,
        "remaining_bytes": 100_000_000_000,
        "device_usage": [],
    },
]


class _Link:
    def __init__(self, servers=None):
        self.servers = _SERVERS if servers is None else servers
        self.commands: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.commands.append((action, args or {}))
        return {}

    async def get_state(self, dst=None):
        return {}


class _Notifier:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []
        self.documents: list = []
        self.photos: list = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None,
                          message_thread_id=None, **kw):
        self.sent.append((chat_id, text, reply_markup))
        return 1

    async def send_document(self, *a, **kw):
        self.documents.append((a, kw))

    async def send_photo(self, *a, **kw):
        self.photos.append((a, kw))


@pytest.fixture(autouse=True)
def _patched(monkeypatch):
    vpn_help.reset_throttle()
    calls = {"fanout": []}

    async def resolve(node_link, server=None):
        return Address(node="jeeves", service="vpn")

    async def fanout(node_link, action, args):
        calls["fanout"].append((action, args))
        return [dict(s) for s in node_link.servers]

    monkeypatch.setattr(vpn_nodes, "resolve_vpn_dst", resolve)
    monkeypatch.setattr(vpn_nodes, "fanout", fanout)
    monkeypatch.setattr(vpn_nodes, "remember_servers", lambda answered: None)
    monkeypatch.setattr(vpn_nodes, "unavailable_servers", lambda answered: [])
    return calls


def _book():
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=ADMIN_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Наталья (@nava40a)",
                chat_id=999222111,
                allowed_commands=["usage@vpn", "issue@vpn"],
                invited_user="Наталья (@nava40a)",
            )
        ],
    )


def _ctx(sub=GUEST, chat_id=GUEST_CHAT, link=None, notifier=None, **over):
    kw = dict(
        chat_id=chat_id,
        dialogue_id=1,
        trigger_message_id=1,
        settings=Settings(),
        node_link=link or _Link(),
        subscription=sub,
        book=_book(),
        notifier=notifier if notifier is not None else _Notifier(),
        author="Гость",
    )
    kw.update(over)
    return ai_tools.ToolContext(**kw)


def _no_secrets(text: str) -> None:
    for marker in SECRETS:
        assert marker not in text


async def test_status_has_countries_quota_devices_no_secrets():
    ctx = _ctx()
    out = await ai_tools.tool_vpn(ctx, {"action": "usage"})
    assert "Нидерланды" in out and "✅" in out
    assert "ГБ" in out
    assert "«iPhone»" in out and "9.1 ГБ" in out
    assert "<b>" not in out
    _no_secrets(out)
    assert ctx.node_link.commands == []  # статус — только fanout usage


async def test_status_without_devices():
    ctx = _ctx(link=_Link([{**_SERVERS[0], "device_usage": []}]))
    out = await ai_tools.tool_vpn(ctx, {"action": "usage"})
    assert "Устройств пока нет" in out


async def test_issue_sends_wizard_button_not_keys():
    notifier = _Notifier()
    ctx = _ctx(notifier=notifier)
    out = await ai_tools.tool_vpn(ctx, {"action": "issue", "device_label": "ignored"})
    assert out.startswith("готово")
    chat, text, markup = notifier.sent[0]
    assert chat == GUEST_CHAT
    buttons = [b.text for row in markup.inline_keyboard for b in row]
    assert "📱 Подключить по шагам" in buttons
    assert ctx.node_link.commands == []  # ни issue, ни reissue в службу
    assert not notifier.documents and not notifier.photos
    _no_secrets(out)


async def test_issue_refused_in_group_and_for_probe():
    notifier = _Notifier()
    out = await ai_tools.tool_vpn(_ctx(chat_id=-100, notifier=notifier), {"action": "issue"})
    assert out.startswith("недоступно") and not notifier.sent
    out = await ai_tools.tool_vpn(_ctx(chat_id=0, notifier=notifier), {"action": "issue"})
    assert out.startswith("недоступно") and not notifier.sent


async def test_issue_without_vless_server():
    link = _Link([{**_SERVERS[0], "transports": ["awg"]}])
    out = await ai_tools.tool_vpn(_ctx(link=link), {"action": "issue"})
    assert out.startswith("недоступно")


async def test_issue_for_recipient_admin_only_and_goes_to_recipient():
    notifier = _Notifier()
    out = await ai_tools.tool_vpn(
        _ctx(notifier=notifier), {"action": "issue", "recipient": "Наталья"}
    )
    assert out.startswith("недоступно") and not notifier.sent
    ctx = _ctx(sub=ADMIN, chat_id=ADMIN_CHAT, notifier=notifier)
    out = await ai_tools.tool_vpn(ctx, {"action": "issue", "recipient": "Наталья"})
    assert out.startswith("готово")
    assert notifier.sent[0][0] == 999222111  # не тому, кто просил


async def test_issue_unknown_recipient():
    ctx = _ctx(sub=ADMIN, chat_id=ADMIN_CHAT)
    out = await ai_tools.tool_vpn(ctx, {"action": "issue", "recipient": "Незнакомый"})
    assert out.startswith("не получилось")


async def test_settings_sends_pick_screen_of_device():
    notifier = _Notifier()
    out = await ai_tools.tool_vpn(
        _ctx(notifier=notifier), {"action": "settings", "device_label": "iphone"}
    )
    assert out.startswith("готово")
    chat, text, markup = notifier.sent[0]
    assert chat == GUEST_CHAT and "iPhone" in text and "чем подключаться" in text
    assert not notifier.documents and not notifier.photos
    _no_secrets(out + text)


async def test_settings_unknown_device_lists_existing():
    out = await ai_tools.tool_vpn(_ctx(), {"action": "settings", "device_label": "Nokia"})
    assert "не нашёл" in out and "iPhone" in out


async def test_settings_private_only():
    out = await ai_tools.tool_vpn(
        _ctx(chat_id=-1), {"action": "settings", "device_label": "iPhone"}
    )
    assert out.startswith("недоступно")


async def test_report_goes_to_admin_with_context_and_throttles():
    notifier = _Notifier()
    ctx = _ctx(notifier=notifier)
    args = {"action": "report", "reason": "не грузит <b>сайты</b>", "device_label": "iPhone"}
    out = await ai_tools.tool_vpn(ctx, args)
    assert out.startswith("готово")
    chat, text, markup = notifier.sent[0]
    assert chat == ADMIN_CHAT
    assert "проблема у" in text and "Гость" in text
    assert "&lt;b&gt;" in text and "со слов человека" in text
    assert "Устройство: iPhone" in text
    assert any("Ответить" in b.text for row in markup.inline_keyboard for b in row)
    out2 = await ai_tools.tool_vpn(ctx, args)
    assert "уже передавали" in out2
    assert len(notifier.sent) == 1


async def test_report_needs_reason_and_not_for_probe():
    out = await ai_tools.tool_vpn(_ctx(), {"action": "report"})
    assert out.startswith("ошибка")
    notifier = _Notifier()
    out = await ai_tools.tool_vpn(
        _ctx(chat_id=0, notifier=notifier), {"action": "report", "reason": "x"}
    )
    assert out.startswith("недоступно") and not notifier.sent


async def test_rights_follow_buttons():
    only_usage = Subscription(
        chat_id=GUEST_CHAT, name="g", allowed_commands=frozenset({"usage@vpn"})
    )
    decl = next(
        d for d in ai_tools.tools_for(only_usage).declarations if d["function"]["name"] == "vpn"
    )
    enum = decl["function"]["parameters"]["properties"]["action"]["enum"]
    assert enum == ["usage", "report"]  # report — по usage@vpn, settings — нет vpn_card
    out = await ai_tools.tool_vpn(_ctx(sub=only_usage), {"action": "issue"})
    assert out.startswith("не умею")
    assert (
        await ai_tools.tool_vpn(_ctx(sub=only_usage), {"action": "settings", "device_label": "x"})
    ).startswith("не умею")
    assert (await ai_tools.tool_vpn(_ctx(sub=only_usage), {"action": "reissue"})).startswith(
        "не умею"
    )


async def test_usage_all_guests_requires_admin():
    out = await ai_tools.tool_vpn(_ctx(), {"action": "usage", "all_guests": True})
    assert out.startswith("недоступно")


async def test_grant_extra_ceiling_and_probe():
    class _Raises(_Link):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            raise ai_tools.ProtoError(ai_tools.vpn_protocol.ERR_QUOTA_CEILING, "потолок")

    sub = Subscription(
        chat_id=GUEST_CHAT, name="g", allowed_commands=frozenset({"grant_extra@vpn"})
    )
    out = await ai_tools.tool_vpn(_ctx(sub=sub, link=_Raises()), {"action": "grant_extra"})
    assert "request_extra" in out
    out = await ai_tools.tool_vpn(_ctx(sub=sub, chat_id=0), {"action": "grant_extra"})
    assert out.startswith("недоступно")


async def test_apk_defaults_to_hiddify_stores_only_amnezia_vpn_otherwise():
    notifier = _Notifier()
    sub = Subscription(chat_id=GUEST_CHAT, name="g", allowed_commands=frozenset({"apk@vpn"}))
    out = await ai_tools.tool_vpn(_ctx(sub=sub, notifier=notifier), {"action": "apk"})
    assert out.startswith("готово")
    urls = [b.url for row in notifier.sent[0][2].inline_keyboard for b in row if b.url]
    assert any("hiddify" in u.lower() for u in urls)
    await ai_tools.tool_vpn(_ctx(sub=sub, notifier=notifier), {"action": "apk", "app": "amnezia"})
    assert "AmneziaVPN" in notifier.sent[1][1]


def test_description_carries_faq_answers():
    decl = next(
        d for d in ai_tools.tools_for(ADMIN).declarations if d["function"]["name"] == "vpn"
    )
    desc = decl["function"]["description"]
    for needle in (
        vpn_facts.ANSWERS[vpn_facts.Q_HIDDIFY],
        "«VLESS · Hiddify»",
        "Из России работает, но на части серверов может не работать",
        "AmneziaVPN",
        ".apk",
        "выберите другую страну",
        "«🔄 Проверить ещё раз»",
        "один телефон или компьютер",
        "переименуйте",
    ):
        assert needle in desc
    assert "бабуш" in desc  # запрет упоминать


def test_status_text_pure():
    assert vpn_agent.status_text([]).startswith("VPN не открыт")
