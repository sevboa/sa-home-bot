from __future__ import annotations

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from sa_home_bot.bot import ai_context
from sa_home_bot.proto.errors import ServiceUnavailableError

from .ai_helpers import user
from .people_helpers import people_book


class NoteMessage:
    def __init__(self, chat_id, chat_type, from_user, reply_to_message=None, quote=None):
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.from_user = from_user
        self.reply_to_message = reply_to_message
        self.quote = quote


def replied(message_id, from_user=None, text=None):
    return SimpleNamespace(message_id=message_id, from_user=from_user, text=text, caption=None)


async def _record_turns(store, *turns):
    now = datetime.now(tz=UTC)
    for message_id, dialogue_id, role, content, user_id, user_name in turns:
        await store.record_ai_turn(
            1, message_id, dialogue_id, role, content, now, user_id=user_id, user_name=user_name
        )


async def _note(store, message, dialogue_id=1):
    return await ai_context.build_context_note(message, store, dialogue_id=dialogue_id)


@pytest.mark.parametrize(
    ("user_args", "expected"),
    [
        (("Иван", None, "ivan"), "Иван (@ivan)"),
        (("Иван", "Иванов", None), "Иван Иванов"),
        (None, None),
    ],
)
def test_display_name(user_args, expected):
    sender = user(*user_args) if user_args else None
    assert ai_context.display_name(sender) == expected


@pytest.mark.parametrize(
    ("birth_date", "today", "age"),
    [
        ("1990-06-15", date(2026, 6, 14), 35),
        ("1990-06-15", date(2026, 6, 15), 36),
        ("1990-06-15", date(2026, 6, 16), 36),
        ("", date(2026, 6, 15), None),
        (None, date(2026, 6, 15), None),
    ],
)
def test_person_age(birth_date, today, age):
    assert ai_context._person_age(birth_date, today) == age


@pytest.mark.parametrize(
    ("birth_date", "expected"), [("1990-04-29", "29 апреля"), ("", None), (None, None)]
)
def test_person_birthday_ru(birth_date, expected):
    assert ai_context._person_birthday_ru(birth_date) == expected


def test_speaker_life_note_includes_local_time_age_and_birthday():
    people = people_book(
        cards={
            42: {
                "gender": "f",
                "city": "Москва",
                "timezone": "Europe/Moscow",
                "birth_date": "1990-06-15",
            }
        }
    )
    note = ai_context._speaker_life_note(people, 42)
    assert "У неё в Москва сейчас" in note
    assert "часовой пояс" in note
    assert "лет" in note
    assert "15 июня" in note


def test_speaker_life_note_none_without_card():
    assert ai_context._speaker_life_note(people_book(), 42) is None


def test_speaker_life_note_skips_age_when_birth_date_unknown():
    note = ai_context._speaker_life_note(people_book(cards={42: {"timezone": "Asia/Almaty"}}), 42)
    assert "Точный возраст" not in note
    assert "День рождения" not in note
    assert "У собеседника сейчас" in note


async def test_context_note_without_sender_has_both_times_and_failure_warning(store):
    note = await _note(store, NoteMessage(1, "private", None))
    assert "Точное время сейчас" in note
    assert "ТВОЁ (Альфреда) личное время" in note
    assert "не повод придумать" in note
    assert "говорит" not in note


async def test_context_note_private_chat_only_mentions_sender(store):
    note = await _note(store, NoteMessage(1, "private", user("Иван", username="ivan")))
    assert "Иван (@ivan)" in note
    assert "начал" not in note
    assert "также обращались" not in note


async def test_context_note_group_includes_starter_and_other_participants(store):
    ivan = user("Иван", username="ivan", id=10)
    await _record_turns(
        store,
        (500, 500, "user", "привет", 10, ai_context.display_name(ivan)),
        (501, 500, "assistant", "Здравствуйте", None, None),
        (600, 600, "user", "как дела", 20, "Мария"),
    )

    note = await _note(store, NoteMessage(1, "group", user("Пётр", id=30)), dialogue_id=500)

    assert "Пётр" in note
    assert "Иван (@ivan)" in note
    assert "Мария" in note
    assert "чужой тред" in note


async def test_context_note_skips_starter_line_when_same_as_sender(store):
    ivan = user("Иван", username="ivan", id=10)
    await _record_turns(store, (500, 500, "user", "привет", 10, ai_context.display_name(ivan)))

    note = await _note(store, NoteMessage(1, "group", ivan), dialogue_id=500)

    assert note.count("Иван (@ivan)") == 1
    assert "начал" not in note
    assert "чужой тред" not in note


async def test_reply_context_includes_foreign_message_text(store):
    foreign = replied(777, from_user=user("Вася", id=99), text="Когда следующий матч?")
    message = NoteMessage(1, "group", user("Иван", id=10), reply_to_message=foreign)

    note = await _note(store, message, dialogue_id=500)

    assert "Вася" in note
    assert "Когда следующий матч?" in note


async def _own_thread_reply(store, text, quote=None):
    ivan = user("Иван", username="ivan", id=10)
    await _record_turns(
        store,
        (500, 500, "user", "привет", 10, "Иван"),
        (501, 500, "assistant", text, None, None),
    )
    message = NoteMessage(
        1,
        "group",
        ivan,
        reply_to_message=replied(501, text=text),
        quote=SimpleNamespace(text=quote) if quote else None,
    )
    return await _note(store, message, dialogue_id=500)


async def test_reply_context_skips_text_already_in_current_dialogue_history(store):
    note = await _own_thread_reply(store, "Слушаю, сэр")
    assert "Сообщение, на которое сейчас отвечают" not in note


async def test_reply_context_quote_in_own_thread_names_its_source(store):
    note = await _own_thread_reply(store, "не имею привычки", quote="привычки")
    assert "твоего же предыдущего сообщения" in note.lower()
    assert "привычки»" in note
    assert "Сообщение, на которое сейчас отвечают" not in note


async def test_reply_context_truncates_long_foreign_message(store):
    long_text = "Ы" * (ai_context._REPLY_QUOTE_MAX_CHARS + 200)
    foreign = replied(777, text=long_text)
    message = NoteMessage(1, "group", user("Иван", id=10), reply_to_message=foreign)

    note = await _note(store, message, dialogue_id=500)

    assert "…" in note
    assert long_text not in note


async def test_no_reply_context_without_reply_to_message(store):
    note = await _note(store, NoteMessage(1, "private", user("Иван", id=10)), dialogue_id=500)
    assert "отвечают" not in note


async def test_reply_to_alfreds_picture_adds_a_note(store):
    image_id = await store.add_image(
        chat_id=555,
        author=None,
        prompt_ru="кабинет",
        prompt_en="study",
        caption="Мой кабинет",
        width=8,
        height=8,
        colors=32,
        png=b"png",
        now=datetime.now(tz=UTC),
        purpose="photo",
        params=json.dumps({"seen": "камин и сова"}, ensure_ascii=False),
    )
    await store.set_image_sent(image_id, "file-1", 900)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=555), reply_to_message=SimpleNamespace(message_id=900)
    )
    note = await ai_context.replied_picture_note(message, store)
    assert f"#{image_id}" in note and "камин и сова" in note and "look_at_photo" in note
    message.reply_to_message = SimpleNamespace(message_id=901)
    assert await ai_context.replied_picture_note(message, store) is None


@pytest.mark.parametrize("recall", [ai_context.recall_facts, ai_context.recall_graph_facts])
async def test_recall_survives_unreachable_service(recall):
    class AsleepLink:
        async def command(self, *_args, **_kwargs):
            raise ServiceUnavailableError("mycraft спит")

    assert await recall(AsleepLink(), 1, "привет") == []
