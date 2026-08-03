"""bot/handlers/vpn.py: карточка /vpn, кнопки, приватность секрета,
конверсия +100 ГБ → заявка при упоре в потолок самообслуживания."""

from __future__ import annotations

import asyncio

import pytest_asyncio

from sa_home_bot.bot.handlers import vpn as vpn_handlers
from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

ADMIN = Subscription(chat_id=1, name="admin", allowed_commands=frozenset({"*"}))
GUEST = Subscription(
    chat_id=777,
    name="guest",
    allowed_commands=frozenset(
        {"usage@vpn", "issue@vpn", "reissue@vpn", "grant_extra@vpn", "request_extra@vpn"}
    ),
)


class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat = FakeChat(chat_id)
        self.answers: list[str] = []
        self.edits: list[str] = []

    async def answer(self, text, **kwargs):
        self.answers.append(text)

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)


class FakeCallback:
    def __init__(self, data: str, chat_id: int) -> None:
        self.data = data
        self.message = FakeMessage(chat_id)
        self.answered: list[tuple] = []

    async def answer(self, *args, **kwargs):
        self.answered.append((args, kwargs))


class FakeNodeLink:
    def __init__(self, result=None, raises=None) -> None:
        self._result = result if result is not None else {}
        self._raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args or {}))
        if self._raises is not None:
            raise self._raises
        return self._result


class FakeNotifier:
    def __init__(self) -> None:
        self.sent_direct: list[tuple[int, str]] = []
        self.sent_photos: list[tuple[int, object, str | None]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_direct(self, chat_id, text, reply_to_message_id=None, reply_markup=None):
        self.sent_direct.append((chat_id, text))
        return len(self.sent_direct)

    async def send_photo(self, chat_id, photo, *, caption=None):
        self.sent_photos.append((chat_id, photo, caption))
        return len(self.sent_photos)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _config(ttl_s: float = 600.0) -> Settings:
    return Settings(vpn=VpnConfig(config_message_ttl_s=ttl_s))


async def test_grant_extra_redraws_card(monkeypatch):
    link = FakeNodeLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:grant_extra", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST)
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_GRANT_EXTRA in actions
    assert vpn_protocol.ACTION_USAGE in actions  # редрайв карточки


async def test_grant_extra_hits_ceiling_converts_to_request(monkeypatch):
    class ToggleLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            self.calls.append((action, args or {}))
            if action == vpn_protocol.ACTION_GRANT_EXTRA:
                raise ProtoError(vpn_protocol.ERR_QUOTA_CEILING, "потолок")
            return {"request_id": 42, "status": "pending"}

    link = ToggleLink()
    callback = FakeCallback("act:vpn:grant_extra", chat_id=777)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), GUEST)
    assert any("№42" in text for text in callback.message.answers)
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_REQUEST_EXTRA in actions


async def test_issue_in_group_chat_is_refused(monkeypatch):
    link = FakeNodeLink()
    callback = FakeCallback("act:vpn:issue:телефон", chat_id=-1001234)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), GUEST)
    assert link.calls == []  # секрет не выпускался вовсе
    assert callback.answered  # но пользователю ответили (алертом)


async def test_issue_in_private_chat_sends_secret_and_cleans_up():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "",
            "device_label": "телефон",
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue:телефон", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(ttl_s=0.01), GUEST)

    assert notifier.sent_direct
    assert "SECRET" in notifier.sent_direct[0][1]
    assert notifier.sent_direct[0][0] == 777

    await asyncio.sleep(0.05)  # дать фоновой задаче автоудаления отработать
    assert notifier.deleted  # сообщение с секретом удалено сама собой


async def test_resolve_request_approve(monkeypatch):
    link = FakeNodeLink(result={"request_id": 7, "status": "approved"})
    callback = FakeCallback("act:vpn:resolve_request:7_approve", chat_id=1)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), ADMIN)
    action, args = link.calls[0]
    assert action == vpn_protocol.ACTION_RESOLVE_REQUEST
    assert args == {"request_id": 7, "approve": True}
    assert any("одобрена" in text for text in callback.message.answers)


async def test_resolve_request_deny(monkeypatch):
    link = FakeNodeLink(result={"request_id": 7, "status": "denied"})
    callback = FakeCallback("act:vpn:resolve_request:7_deny", chat_id=1)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), ADMIN)
    action, args = link.calls[0]
    assert args == {"request_id": 7, "approve": False}
    assert any("отклонена" in text for text in callback.message.answers)


@pytest_asyncio.fixture(autouse=True)
async def _drain_pending_tasks():
    yield
    # Собрать любые фоновые задачи автоудаления, оставшиеся от тестов с
    # длинным TTL, чтобы event loop не ругался при закрытии.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in pending:
        task.cancel()
