from __future__ import annotations

from datetime import UTC, datetime

from aiogram.types import Chat, Message, User

from sa_home_bot.bot.reply_queue import FIELD_ATTEMPTS, FIELD_BLOCKED, REPLY_QUEUE_KEY

BASE_TIME = datetime(2026, 6, 22, 12, 0, 0, tzinfo=UTC)

USER = User(id=10, is_bot=False, first_name="Аня", username="anya")


def make_message(message_id: int, text: str, chat_id: int = 1) -> Message:
    return Message(
        message_id=message_id,
        date=datetime.now(tz=UTC),
        chat=Chat(id=chat_id, type="private"),
        from_user=USER,
        text=text,
    )


async def test_put_creates_entry(reply_queue):
    await reply_queue.put(make_message(42, "привет"), 500)
    assert await reply_queue.entries() == {
        "1": {
            "dialogue_id": 500,
            "last_message_id": 42,
            "thread_id": None,
            "chat_type": "private",
            "is_topic": False,
            "user": {"id": 10, "first_name": "Аня", "last_name": None, "username": "anya"},
            "text": "привет",
            "since": BASE_TIME.isoformat(),
            FIELD_ATTEMPTS: 0,
            FIELD_BLOCKED: False,
        }
    }


async def test_put_keeps_old_state(reply_queue):
    await reply_queue.put(make_message(42, "привет"), 500)
    assert await reply_queue.fail(1) == 1
    new = make_message(43, "ещё вопрос", chat_id=1)
    await reply_queue.put(new, 700)
    entry = (await reply_queue.entries())["1"]
    assert entry["dialogue_id"] == 500
    assert entry["since"] == BASE_TIME.isoformat()
    assert entry[FIELD_ATTEMPTS] == 1
    assert entry[FIELD_BLOCKED] is False
    assert entry["last_message_id"] == 43
    assert entry["text"] == "ещё вопрос"


async def test_finish_requires_current_message_id(reply_queue):
    await reply_queue.put(make_message(42, "привет"), 500)
    assert await reply_queue.finish(1, 43) is False
    assert (await reply_queue.entries())["1"]["last_message_id"] == 42
    assert await reply_queue.finish(1, 42) is True
    assert await reply_queue.store.get_state(REPLY_QUEUE_KEY) is None


async def test_finish_keeps_other_chats(reply_queue):
    await reply_queue.put(make_message(42, "привет", chat_id=1), 500)
    await reply_queue.put(make_message(43, "там", chat_id=2), 500)
    assert await reply_queue.finish(1, 42) is True
    assert list(await reply_queue.entries()) == ["2"]


async def test_fail_grows_attempts(reply_queue):
    assert await reply_queue.fail(1) == 0
    assert await reply_queue.store.get_state(REPLY_QUEUE_KEY) is None
    await reply_queue.put(make_message(42, "раз"), 500)
    assert await reply_queue.fail(1) == 1
    assert await reply_queue.fail(1) == 2
    assert (await reply_queue.entries())["1"][FIELD_ATTEMPTS] == 2


async def test_block_marks_first_time(reply_queue):
    assert await reply_queue.block(1) is False
    assert await reply_queue.store.get_state(REPLY_QUEUE_KEY) is None
    await reply_queue.put(make_message(42, "раз"), 500)
    assert await reply_queue.block(1) is True
    assert (await reply_queue.entries())["1"][FIELD_BLOCKED] is True
    assert await reply_queue.block(1) is False
