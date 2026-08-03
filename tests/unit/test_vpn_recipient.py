"""Тул vpn: issue/reissue ДРУГОМУ человеку через recipient.

Живой баг 2026-08-04: без этого параметра тул тихо создавал пир СЕБЕ (тому,
кто просит) с меткой чужого имени в device_label и отправлял секрет туда
же — не тому человеку, о котором просили, а модель ещё и уверяла, что
получатель данные получил. См. bot/tools.py::tool_vpn."""

from __future__ import annotations

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.config import GuestSubscriptionConfig, Settings, SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

ADMIN_CHAT = 1
NATALYA_CHAT = 999222111

ADMIN_SUB = Subscription(chat_id=ADMIN_CHAT, name="admin", allowed_commands=frozenset({"*"}))
NON_ADMIN_SUB = Subscription(
    chat_id=ADMIN_CHAT,
    name="not-admin",
    allowed_commands=frozenset({"usage@vpn", "issue@vpn", "reissue@vpn"}),
)


def _book() -> SubscriptionBook:
    return SubscriptionBook.from_config(
        [SubscriptionConfig(name="me", chat_id=ADMIN_CHAT, allowed_commands=["*"])],
        [
            GuestSubscriptionConfig(
                name="Наталья (@nava40a)",
                chat_id=NATALYA_CHAT,
                allowed_commands=["chat@llm"],
                invited_user="Наталья (@nava40a)",
            )
        ],
    )


class _FakeLink:
    def __init__(self, result=None) -> None:
        self._result = result if result is not None else {
            "config_text": "[Interface]\nPrivateKey = SECRET"
        }
        self.calls: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        return self._result


class _FakeNotifier:
    def __init__(self) -> None:
        self.sent_documents: list[tuple[int, object, str | None]] = []
        self.sent_photos: list[tuple[int, object, str | None]] = []

    async def send_document(
        self, chat_id, document, *, filename=None, caption=None, message_thread_id=None
    ):
        self.sent_documents.append((chat_id, document, caption))
        return (1, "tg-file-id")

    async def send_photo(
        self, chat_id, photo, *, filename=None, caption=None, message_thread_id=None
    ):
        self.sent_photos.append((chat_id, photo, caption))
        return 1


def _ctx(**over):
    defaults = dict(
        chat_id=ADMIN_CHAT,
        dialogue_id=1,
        trigger_message_id=1,
        settings=Settings(),
        node_link=_FakeLink(),
        subscription=ADMIN_SUB,
        book=_book(),
        notifier=_FakeNotifier(),
    )
    defaults.update(over)
    return ai_tools.ToolContext(**defaults)


async def test_issue_for_recipient_requires_admin():
    ctx = _ctx(subscription=NON_ADMIN_SUB)
    result = await ai_tools.tool_vpn(
        ctx, {"action": "issue", "device_label": "телефон", "recipient": "Наталья"}
    )
    assert result.startswith("недоступно")
    assert ctx.node_link.calls == []


async def test_issue_for_recipient_delivers_to_target_chat_not_asker():
    notifier = _FakeNotifier()
    link = _FakeLink()
    ctx = _ctx(notifier=notifier, node_link=link)
    result = await ai_tools.tool_vpn(
        ctx, {"action": "issue", "device_label": "телефон", "recipient": "Наталья"}
    )
    assert "SECRET" not in result
    assert "Наталья" in result
    action, args = link.calls[0]
    assert action == "issue"
    assert args["chat_id"] == NATALYA_CHAT  # не в чат того, кто просил
    assert notifier.sent_documents[0][0] == NATALYA_CHAT
    assert b"SECRET" in notifier.sent_documents[0][1]


async def test_issue_without_recipient_still_targets_self():
    link = _FakeLink()
    ctx = _ctx(node_link=link)
    await ai_tools.tool_vpn(ctx, {"action": "issue", "device_label": "телефон"})
    action, args = link.calls[0]
    assert args["chat_id"] == ADMIN_CHAT


async def test_issue_for_unknown_recipient_refuses():
    link = _FakeLink()
    ctx = _ctx(node_link=link)
    result = await ai_tools.tool_vpn(
        ctx, {"action": "issue", "device_label": "телефон", "recipient": "Незнакомый"}
    )
    assert "не получилось" in result
    assert link.calls == []


async def test_reissue_for_recipient_still_requires_confirm():
    link = _FakeLink()
    ctx = _ctx(node_link=link)
    result = await ai_tools.tool_vpn(
        ctx, {"action": "reissue", "device_label": "телефон", "recipient": "Наталья"}
    )
    assert "confirm" in result
    assert link.calls == []


async def test_reissue_for_recipient_with_confirm_targets_recipient_chat():
    link = _FakeLink()
    ctx = _ctx(node_link=link)
    result = await ai_tools.tool_vpn(
        ctx,
        {
            "action": "reissue",
            "device_label": "телефон",
            "recipient": "Наталья",
            "confirm": True,
        },
    )
    assert "Наталья" in result
    action, args = link.calls[0]
    assert args["chat_id"] == NATALYA_CHAT
