"""bot/handlers/vpn.py: карточка /vpn, кнопки, приватность секрета,
конверсия +100 ГБ → заявка при упоре в потолок самообслуживания."""

from __future__ import annotations

import asyncio

import pytest_asyncio

from sa_home_bot.bot.handlers import vpn as vpn_handlers
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
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
        self.answer_markups: list[object] = []
        self.edits: list[str] = []
        self.message_thread_id: int | None = None
        self.reply_markup_cleared = False

    async def answer(self, text, reply_markup=None, **kwargs):
        self.answers.append(text)
        self.answer_markups.append(reply_markup)

    async def edit_text(self, text, **kwargs):
        self.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None, **kwargs):
        if reply_markup is None:
            self.reply_markup_cleared = True


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
        self.sent_direct_markups: list[object] = []
        self.sent_documents: list[tuple[int, object, str | None]] = []
        self.sent_photos: list[tuple[int, object, str | None]] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_direct(
        self,
        chat_id,
        text,
        reply_to_message_id=None,
        reply_markup=None,
        message_thread_id=None,
    ):
        self.sent_direct.append((chat_id, text))
        self.sent_direct_markups.append(reply_markup)
        return len(self.sent_direct)

    async def send_document(
        self, chat_id, document, *, filename=None, caption=None, message_thread_id=None
    ):
        self.sent_documents.append((chat_id, document, caption))
        return (len(self.sent_documents), "tg-file-id")

    async def send_photo(
        self, chat_id, photo, *, filename=None, caption=None, message_thread_id=None
    ):
        self.sent_photos.append((chat_id, photo, caption))
        return len(self.sent_photos)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


def _config(ttl_s: float = 600.0) -> Settings:
    return Settings(vpn=VpnConfig(config_message_ttl_s=ttl_s))


def _pending() -> PendingVpnSecrets:
    return PendingVpnSecrets()


async def test_grant_extra_redraws_card(monkeypatch):
    link = FakeNodeLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:grant_extra", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, _pending())
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
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), GUEST, _pending())
    assert any("№42" in text for text in callback.message.answers)
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_REQUEST_EXTRA in actions


async def test_revoke_calls_service_and_redraws_card():
    link = FakeNodeLink(result={"used_bytes": 0, "limit_bytes": 1, "remaining_bytes": 1})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:revoke:Rose", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, _pending())
    action, args = link.calls[0]
    assert action == vpn_protocol.ACTION_REVOKE
    assert args == {"chat_id": 777, "device_label": "Rose"}
    actions = [c[0] for c in link.calls]
    assert vpn_protocol.ACTION_USAGE in actions  # редрайв карточки
    assert any("Rose" in str(a) for a in callback.answered)


async def test_revoke_without_label_is_noop():
    link = FakeNodeLink()
    callback = FakeCallback("act:vpn:revoke", chat_id=777)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), GUEST, _pending())
    assert link.calls == []
    assert callback.answered


async def test_card_keyboard_offers_revoke_button_per_device():
    keyboard = vpn_handlers._card_keyboard(
        [{"device_label": "Rose"}], is_admin=False, can_self_serve=False
    )
    device_row = keyboard.inline_keyboard[1]
    texts = [button.text for button in device_row]
    assert any("Отозвать" in text for text in texts)
    assert any("Перевыпустить" in text for text in texts)


async def test_apk_first_click_shows_links_not_file():
    link = FakeNodeLink()
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:apk", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, _pending())
    text = next(t for t in callback.message.answers if "App Store" in t)
    assert "Google Play" in text
    # Обе ссылки по магазину — текстом ("AmneziaVPN"/"AmneziaWG"), не длинным URL.
    cfg = _config().vpn
    assert f'<a href="{cfg.amneziavpn_ios_app_store_url}">AmneziaVPN</a>' in text
    assert f'<a href="{cfg.ios_app_store_url}">AmneziaWG</a>' in text
    assert f'<a href="{cfg.amneziavpn_google_play_url}">AmneziaVPN</a>' in text
    assert f'<a href="{cfg.google_play_url}">AmneziaWG</a>' in text
    assert link.calls == []  # ссылки статические — служба вообще не спрошена
    assert notifier.sent_documents == []  # файл ещё не ушёл


async def test_apk_send_click_delivers_file_and_hides_button():
    link = FakeNodeLink(result={"telegram_file_id": "cached-id", "version": "2.0.1"})
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:apk:send", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, _pending())
    assert notifier.sent_documents[0][:2] == (777, "cached-id")
    assert callback.message.reply_markup_cleared


async def test_issue_in_group_chat_is_refused(monkeypatch):
    link = FakeNodeLink()
    callback = FakeCallback("act:vpn:issue", chat_id=-1001234)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), GUEST, _pending())
    assert link.calls == []  # секрет не выпускался вовсе
    assert callback.answered  # но пользователю ответили (алертом)


async def test_issue_first_device_sends_file_first_then_qr_button():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",  # непустой — фейковый PNG в base64 ("qr")
            "device_label": "Rose",
            "prior_device_count": 0,  # первое устройство чата
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, _pending())

    # Первое устройство чата — файл ушёл сразу, QR — ещё нет (решение
    # пользователя 2026-08-04: скорее всего настраивается прямо с этого
    # телефона).
    assert notifier.sent_documents and notifier.sent_documents[0][0] == 777
    assert b"SECRET" in notifier.sent_documents[0][1]
    assert notifier.sent_photos == []
    assert notifier.sent_direct and notifier.sent_direct[0][0] == 777
    assert notifier.sent_direct_markups[0] is not None  # кнопка «Дать QR-код»


async def test_issue_second_device_sends_qr_first_then_config_button():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 1,  # уже есть хотя бы одно устройство
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, _pending())

    # Второе и последующие устройства — обычно для другого устройства/
    # человека, поэтому QR уходит сразу, а файл — за кнопкой.
    assert notifier.sent_photos and notifier.sent_photos[0][0] == 777
    assert notifier.sent_documents == []
    assert notifier.sent_direct and notifier.sent_direct[0][0] == 777
    assert notifier.sent_direct_markups[0] is not None  # кнопка «Дать файл конфига»


async def test_config_button_delivers_file_and_hides_itself():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "",
            "device_label": "Rose",
            "prior_device_count": 1,  # QR первым, файл — по кнопке
        }
    )
    notifier = FakeNotifier()
    pending = _pending()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, pending)

    button_markup = notifier.sent_direct_markups[0]
    token_button = button_markup.inline_keyboard[0][0]
    callback2 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback2, link, notifier, _config(), GUEST, pending)

    assert notifier.sent_documents  # секрет ушёл файлом .conf, не текстом
    assert b"SECRET" in notifier.sent_documents[0][1]
    assert notifier.sent_documents[0][0] == 777
    assert callback2.message.reply_markup_cleared  # кнопка спряталась


async def test_config_button_reveals_qr_when_file_was_first():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 0,  # файл первым, QR — по кнопке
        }
    )
    notifier = FakeNotifier()
    pending = _pending()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, pending)

    button_markup = notifier.sent_direct_markups[0]
    token_button = button_markup.inline_keyboard[0][0]
    callback2 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback2, link, notifier, _config(), GUEST, pending)

    assert notifier.sent_photos  # секрет ушёл QR-картинкой, не файлом
    assert notifier.sent_photos[0][0] == 777
    assert callback2.message.reply_markup_cleared  # кнопка спряталась


async def test_config_button_used_twice_refuses_second_time():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "",
            "device_label": "Rose",
            "prior_device_count": 1,
        }
    )
    notifier = FakeNotifier()
    pending = _pending()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(callback, link, notifier, _config(), GUEST, pending)
    token_button = notifier.sent_direct_markups[0].inline_keyboard[0][0]

    callback2 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback2, link, notifier, _config(), GUEST, pending)
    callback3 = FakeCallback(token_button.callback_data, chat_id=777)
    await vpn_handlers.handle_action(callback3, link, notifier, _config(), GUEST, pending)

    assert len(notifier.sent_documents) == 1  # второй раз файл не ушёл
    assert callback3.answered  # но ответ (об устаревшей ссылке) есть


async def test_issue_secret_cleans_up_after_ttl_without_click():
    link = FakeNodeLink(
        result={
            "config_text": "[Interface]\nPrivateKey = SECRET",
            "qr_png_b64": "cXI=",
            "device_label": "Rose",
            "prior_device_count": 1,
        }
    )
    notifier = FakeNotifier()
    callback = FakeCallback("act:vpn:issue", chat_id=777)
    await vpn_handlers.handle_action(
        callback, link, notifier, _config(ttl_s=0.01), GUEST, _pending()
    )

    await asyncio.sleep(0.05)  # дать фоновой задаче автоудаления отработать
    assert len(notifier.deleted) == 2  # сообщение с секретом и сообщение с кнопкой


async def test_resolve_request_approve(monkeypatch):
    link = FakeNodeLink(result={"request_id": 7, "status": "approved"})
    callback = FakeCallback("act:vpn:resolve_request:7_approve", chat_id=1)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), ADMIN, _pending())
    action, args = link.calls[0]
    assert action == vpn_protocol.ACTION_RESOLVE_REQUEST
    assert args == {"request_id": 7, "approve": True}
    assert any("одобрена" in text for text in callback.message.answers)


async def test_resolve_request_deny(monkeypatch):
    link = FakeNodeLink(result={"request_id": 7, "status": "denied"})
    callback = FakeCallback("act:vpn:resolve_request:7_deny", chat_id=1)
    await vpn_handlers.handle_action(callback, link, FakeNotifier(), _config(), ADMIN, _pending())
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
