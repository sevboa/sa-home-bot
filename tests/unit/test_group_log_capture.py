from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    Animation,
    Audio,
    Chat,
    Contact,
    Document,
    Location,
    Message,
    PhotoSize,
    Poll,
    Sticker,
    Update,
    User,
    Video,
    VideoNote,
    Voice,
)

from sa_home_bot.bot.ai_turns import PHOTO_MARKER, STICKER_MARKER
from sa_home_bot.bot.group_log_capture import (
    GroupLogMiddleware,
    author_of,
    edited_message_handler,
    message_content,
    reply_target,
)
from sa_home_bot.bot.invites import Gatekeeper, InvitesConfig
from sa_home_bot.bot.middlewares import SilenceGate
from sa_home_bot.config import SubscriptionConfig
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.guests import GuestStore
from tests.unit.support.dispatch import prod_dispatcher

BASE_TIME = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)


def _book(
    *,
    admin_chat: int = 1,
    group_log_chat: int | None = None,
    other_group_chat: int | None = None,
) -> SubscriptionBook:
    configs = [SubscriptionConfig(name="admin", chat_id=admin_chat, allowed_commands=["*"])]
    if other_group_chat is not None:
        configs.append(
            SubscriptionConfig(
                name="other", chat_id=other_group_chat, allowed_commands=["chat@llm"]
            )
        )
    if group_log_chat is not None:
        configs.append(
            SubscriptionConfig(
                name="group_log",
                chat_id=group_log_chat,
                allowed_commands=["chat@llm"],
            )
        )
    group_log_ids = [group_log_chat] if group_log_chat is not None else []
    return SubscriptionBook.from_config(configs, group_log_chat_ids=group_log_ids)


def _gate(store, tmp_path, book=None) -> Gatekeeper:
    return Gatekeeper(
        InvitesConfig(),
        store,
        book or _book(),
        GuestStore(tmp_path / "telegram-bot.test.guests.toml"),
        None,
    )


def _msg(**fields) -> Message:
    chat = fields.pop("chat", Chat(id=-100, type="supergroup"))
    return Message(
        message_id=fields.pop("message_id", 1),
        date=BASE_TIME,
        chat=chat,
        **fields,
    )


class TestMessageContent:
    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({"text": "hello"}, "hello"),
            (
                {
                    "photo": [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)],
                    "caption": "nice photo",
                },
                f"{PHOTO_MARKER} nice photo",
            ),
            (
                {"photo": [PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]},
                PHOTO_MARKER,
            ),
            (
                {
                    "sticker": Sticker(
                        file_id="f",
                        file_unique_id="u",
                        type="regular",
                        width=1,
                        height=1,
                        is_animated=False,
                        is_video=False,
                        emoji="🙂",
                    )
                },
                f"{STICKER_MARKER} 🙂",
            ),
            (
                {
                    "sticker": Sticker(
                        file_id="f",
                        file_unique_id="u",
                        type="regular",
                        width=1,
                        height=1,
                        is_animated=False,
                        is_video=False,
                        emoji="",
                    )
                },
                STICKER_MARKER,
            ),
            ({"voice": Voice(file_id="f", file_unique_id="u", duration=1)}, "[голосовое]"),
            (
                {"voice": Voice(file_id="f", file_unique_id="u", duration=1), "caption": "слушай"},
                "[голосовое] слушай",
            ),
            (
                {
                    "video": Video(file_id="f", file_unique_id="u", width=1, height=1, duration=1),
                    "caption": "смотри",
                },
                "[видео] смотри",
            ),
            (
                {"document": Document(file_id="f", file_unique_id="u"), "caption": "отчёт"},
                "[документ] отчёт",
            ),
            (
                {"video_note": VideoNote(file_id="f", file_unique_id="u", length=1, duration=1)},
                "[кружочек]",
            ),
            ({"audio": Audio(file_id="f", file_unique_id="u", duration=1)}, "[аудио]"),
            (
                {"video": Video(file_id="f", file_unique_id="u", width=1, height=1, duration=1)},
                "[видео]",
            ),
            (
                {
                    "animation": Animation(
                        file_id="f", file_unique_id="u", width=1, height=1, duration=1
                    )
                },
                "[гифка]",
            ),
            ({"document": Document(file_id="f", file_unique_id="u")}, "[документ]"),
            (
                {
                    "poll": Poll(
                        id="p",
                        question="Куда?",
                        options=[],
                        total_voter_count=0,
                        is_closed=False,
                        is_anonymous=True,
                        type="regular",
                        allows_multiple_answers=False,
                        allows_revoting=False,
                        members_only=False,
                    )
                },
                "[опрос: Куда?]",
            ),
            (
                {
                    "poll": Poll(
                        id="p",
                        question="",
                        options=[],
                        total_voter_count=0,
                        is_closed=False,
                        is_anonymous=True,
                        type="regular",
                        allows_multiple_answers=False,
                        allows_revoting=False,
                        members_only=False,
                    )
                },
                "[опрос]",
            ),
            ({"location": Location(latitude=1.0, longitude=1.0)}, "[геолокация]"),
            ({"contact": Contact(phone_number="1", first_name="A")}, "[контакт]"),
            ({"new_chat_members": [User(id=2, is_bot=False, first_name="B")]}, None),
            ({"left_chat_member": User(id=2, is_bot=False, first_name="B")}, None),
            (
                {
                    "pinned_message": Message(
                        message_id=2,
                        date=BASE_TIME,
                        chat=Chat(id=-100, type="supergroup"),
                    )
                },
                None,
            ),
        ],
    )
    def test_message_content(self, fields: dict, expected: str | None):
        msg = _msg(**fields)
        assert message_content(msg) == expected


class TestReplyTarget:
    def test_reply_to_regular_message(self):
        reply = Message(message_id=7, date=BASE_TIME, chat=Chat(id=-100, type="supergroup"))
        msg = _msg(reply_to_message=reply)
        assert reply_target(msg) == 7

    def test_no_reply(self):
        msg = _msg()
        assert reply_target(msg) is None

    def test_reply_to_topic_root_returns_none(self):
        reply = Message(message_id=7, date=BASE_TIME, chat=Chat(id=-100, type="supergroup"))
        msg = _msg(reply_to_message=reply, is_topic_message=True, message_thread_id=7)
        assert reply_target(msg) is None

    def test_reply_to_other_message_in_topic(self):
        reply = Message(message_id=8, date=BASE_TIME, chat=Chat(id=-100, type="supergroup"))
        msg = _msg(reply_to_message=reply, is_topic_message=True, message_thread_id=7)
        assert reply_target(msg) == 8


class TestAuthorOf:
    def test_from_user(self):
        user = User(id=10, is_bot=False, first_name="Test User", username="testuser")
        msg = _msg(from_user=user)
        user_id, user_name = author_of(msg)
        assert user_id == 10
        assert user_name == "Test User (@testuser)"

    def test_from_user_no_username(self):
        user = User(id=10, is_bot=False, first_name="Test User")
        msg = _msg(from_user=user)
        user_id, user_name = author_of(msg)
        assert user_id == 10
        assert user_name == "Test User"

    def test_sender_chat(self):
        sender_chat = Chat(id=100, type="channel", title="Channel Title")
        msg = _msg(from_user=None, sender_chat=sender_chat)
        user_id, user_name = author_of(msg)
        assert user_id == 100
        assert user_name == "Channel Title"

    def test_no_author(self):
        msg = _msg(from_user=None, sender_chat=None)
        user_id, user_name = author_of(msg)
        assert user_id is None
        assert user_name is None


def _dispatcher(book, store, tmp_path, *, with_spy: bool = True):
    gate = _gate(store, tmp_path, book)
    dp = Dispatcher()
    dp.update.outer_middleware(SilenceGate(book, gate))
    dp.message.outer_middleware(GroupLogMiddleware(book, store))
    dp.edited_message.register(edited_message_handler(book, store))

    seen: list[str] = []
    if not with_spy:
        return dp, seen, gate
    spy = Router(name="spy")

    @spy.message()
    async def _catch_all(message: Message) -> None:
        seen.append(message.text or "")

    dp.include_router(spy)
    return dp, seen, gate


class TestGroupLogCapture:
    @pytest.mark.asyncio
    async def test_message_in_group_with_group_log_recorded(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, seen, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            user = User(id=10, is_bot=False, first_name="Test User", username="testuser")
            msg = _msg(text="hello group", chat=Chat(id=-100, type="group"), from_user=user)
            update = Update(update_id=1, message=msg)
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

        rows = await store.recent_group_messages(-100, limit=10)
        assert len(rows) == 1
        assert rows[0]["content"] == "hello group"
        assert rows[0]["user_id"] == 10
        assert rows[0]["user_name"] == "Test User (@testuser)"
        assert rows[0]["reply_to"] is None
        assert rows[0]["from_alfred"] == 0

    @pytest.mark.asyncio
    async def test_message_without_any_handler_recorded(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, _, _ = _dispatcher(book, store, tmp_path, with_spy=False)
        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(text="nobody handles", chat=Chat(id=-100, type="group"))
            await dp.feed_update(bot, Update(update_id=1, message=msg))
        finally:
            await bot.session.close()

        rows = await store.recent_group_messages(-100, limit=10)
        assert [row["content"] for row in rows] == ["nobody handles"]

    @pytest.mark.asyncio
    async def test_message_in_group_without_group_log_not_recorded(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100, other_group_chat=-200)
        dp, seen, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(text="hello other group", chat=Chat(id=-200, type="group"))
            await dp.feed_update(bot, Update(update_id=1, message=msg))
        finally:
            await bot.session.close()

        assert seen == ["hello other group"]
        assert await store.recent_group_messages(-200, limit=10) == []

    @pytest.mark.asyncio
    async def test_private_message_not_recorded(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, seen, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(text="hello private", chat=Chat(id=1, type="private"))
            await dp.feed_update(bot, Update(update_id=1, message=msg))
        finally:
            await bot.session.close()

        assert seen == ["hello private"]
        assert await store.recent_group_messages(1, limit=10) == []

    @pytest.mark.asyncio
    async def test_chat_without_subscription_not_recorded(self, store, tmp_path):
        book = _book(admin_chat=1)
        dp, _, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(text="hello unknown", chat=Chat(id=999, type="group"))
            update = Update(update_id=1, message=msg)
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

        rows = await store.recent_group_messages(999, limit=10)
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_record_failure_does_not_block_handler(self, store, tmp_path, monkeypatch):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, seen, _ = _dispatcher(book, store, tmp_path)

        async def failing_record(*args, **kwargs):
            raise RuntimeError("db error")

        monkeypatch.setattr(store, "record_group_message", failing_record)

        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(text="hello group", chat=Chat(id=-100, type="group"))
            update = Update(update_id=1, message=msg)
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

        assert seen == ["hello group"]

    @pytest.mark.asyncio
    async def test_edited_message_updates_content(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, _, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(text="original", chat=Chat(id=-100, type="group"))
            update = Update(update_id=1, message=msg)
            await dp.feed_update(bot, update)

            edited = _msg(text="edited", chat=Chat(id=-100, type="group"))
            update = Update(update_id=2, edited_message=edited)
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

        rows = await store.recent_group_messages(-100, limit=10)
        assert len(rows) == 1
        assert rows[0]["content"] == "edited"

    @pytest.mark.asyncio
    async def test_edited_unknown_message_does_not_create_row(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, _, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            edited = _msg(text="edited unknown", chat=Chat(id=-100, type="group"), message_id=999)
            update = Update(update_id=1, edited_message=edited)
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

        rows = await store.recent_group_messages(-100, limit=10)
        assert len(rows) == 0

    @pytest.mark.asyncio
    async def test_migrate_to_chat_id_logs_warning(self, store, tmp_path, caplog):
        book = _book(admin_chat=1, group_log_chat=-100)
        dp, _, _ = _dispatcher(book, store, tmp_path)
        bot = Bot("123456:fake-token-for-tests")

        try:
            msg = _msg(migrate_to_chat_id=200, chat=Chat(id=-100, type="group"))
            update = Update(update_id=1, message=msg)
            await dp.feed_update(bot, update)
        finally:
            await bot.session.close()

        assert any("Миграция чата" in record.message for record in caplog.records)
        rows = await store.recent_group_messages(-100, limit=10)
        assert len(rows) == 0


class TestDispatcherWiring:
    def test_group_log_middleware_and_edited_message_registered(self, store, tmp_path):
        book = _book(admin_chat=1, group_log_chat=-100)
        gate = _gate(store, tmp_path, book)

        with prod_dispatcher(book, gate, store) as dp:
            assert any(isinstance(m, GroupLogMiddleware) for m in dp.message.outer_middleware)
            assert "edited_message" in dp.resolve_used_update_types()
