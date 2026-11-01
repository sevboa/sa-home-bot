from __future__ import annotations

import json

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from sa_home_bot.bot import ai_lines, invites
from sa_home_bot.bot.devlab_notify import NOTICE_KEY, REPLY_KEY, encode_entry
from sa_home_bot.config import InvitesConfig, Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.subscriptions.guests import GuestStore
from tests.unit.support.dispatch import BOT_USER, prod_dispatcher, recording_bot
from tests.unit.support.factories import make_book, make_subscription
from tests.unit.support.fakes import FakeKicker, FakeNodeLink, FakeNotifier

from .conftest import BASE_TIME

OWNER = 188548043
NOTICE_ID = 26854
PROMPT_ID = 26855

CHAT = Chat(id=OWNER, type="private")
OWNER_USER = User(id=OWNER, is_bot=False, first_name="Владелец")


def waiting_link():
    return FakeNodeLink(
        results={
            protocol.ACTION_OWNER_JOB: {protocol.FIELD_STATUS: protocol.STATUS_WAITING_OWNER},
            protocol.ACTION_OWNER_POST: {protocol.FIELD_STATUS: protocol.STATUS_RUNNING},
        }
    )


def bot_message(message_id, text):
    return Message(message_id=message_id, date=BASE_TIME, chat=CHAT, from_user=BOT_USER, text=text)


def reply_update(update_id, reply_to_id, text):
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id + 30000,
            date=BASE_TIME,
            chat=CHAT,
            from_user=OWNER_USER,
            text=text,
            reply_to_message=bot_message(reply_to_id, "вопрос агента"),
        ),
    )


def button_update(update_id, data):
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=OWNER_USER,
            chat_instance="ci",
            data=data,
            message=bot_message(NOTICE_ID, "вопрос агента"),
        ),
    )


@pytest.fixture
async def live(store, tmp_path):
    book = make_book(make_subscription(OWNER, "owner"))
    notifier = FakeNotifier()
    gate = invites.Gatekeeper(
        InvitesConfig(), store, book, GuestStore(tmp_path / "guests.toml"), notifier
    )
    link = waiting_link()
    kicker = FakeKicker()
    bot, session = recording_bot()
    workflow = {
        "store": store,
        "node_link": link,
        "config": Settings(),
        "notifier": notifier,
        "book": book,
        "gate": gate,
        "devlab_notifier": kicker,
    }
    with prod_dispatcher(book, gate, store) as dp:

        async def feed(update):
            await dp.feed_update(bot, update, **workflow)

        yield feed, link, session, kicker
    await bot.session.close()


async def test_reply_to_force_reply_prompt_reaches_hub(live, store):
    feed, link, session, kicker = live
    await store.set_state(
        REPLY_KEY.format(chat_id=OWNER, message_id=PROMPT_ID),
        encode_entry("cgpkp2", user_id=OWNER),
    )
    await feed(reply_update(1, PROMPT_ID, "go"))
    assert link.commands == [
        (
            protocol.ACTION_OWNER_POST,
            {
                protocol.FIELD_ID: "cgpkp2",
                protocol.FIELD_KIND: protocol.MSG_ANSWER,
                protocol.PAYLOAD_TEXT: "go",
            },
        )
    ]
    assert [m.text for m in session.sent(SendMessage)] == [ai_lines.DEVLAB_SENT_TEXT]
    assert kicker.kicked == 1


async def test_reply_to_question_notice_reaches_hub(live, store):
    feed, link, _, _ = live
    notice_key = NOTICE_KEY.format(chat_id=OWNER, message_id=NOTICE_ID)
    await store.set_state(notice_key, encode_entry("j9"))
    await feed(reply_update(2, NOTICE_ID, "ответ"))
    assert link.commands[0][1][protocol.FIELD_ID] == "j9"


async def test_answer_button_then_reply_goes_to_hub(live, store):
    feed, link, session, _ = live
    await feed(button_update(3, "dl:ans:cgpkp2"))
    assert session.sent(AnswerCallbackQuery)
    prompt = session.sent(SendMessage)[0]
    assert prompt.text == ai_lines.DEVLAB_REPLY_PROMPT.format(job_id="cgpkp2")
    keys = await store.state_keys("devlab_reply:")
    assert len(keys) == 1
    prompt_id = int(keys[0].rsplit(":", 1)[1])
    assert json.loads(await store.get_state(keys[0]))["user_id"] == OWNER
    await feed(reply_update(4, prompt_id, "go"))
    assert link.commands[-1][0] == protocol.ACTION_OWNER_POST
    assert link.commands[-1][1][protocol.PAYLOAD_TEXT] == "go"


async def test_release_button_posts_to_hub(live):
    feed, link, session, _ = live
    await feed(button_update(5, "dl:rel:cgpkp2"))
    assert link.commands == [
        (
            protocol.ACTION_OWNER_POST,
            {protocol.FIELD_ID: "cgpkp2", protocol.FIELD_KIND: protocol.MSG_RELEASE},
        )
    ]
