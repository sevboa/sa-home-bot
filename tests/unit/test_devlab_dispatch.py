from __future__ import annotations

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from sa_home_bot.bot import ai_lines, invites
from sa_home_bot.bot.away import KIND_TEXT
from sa_home_bot.bot.middlewares import DENIED_ALERT_TEXT
from sa_home_bot.config import InvitesConfig, Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.subscriptions.guests import GuestStore
from tests.unit.support.dispatch import BOT_USER, prod_dispatcher, recording_bot
from tests.unit.support.factories import make_book, make_subscription
from tests.unit.support.fakes import FakeKicker, FakeNodeLink, FakeNotifier

from .conftest import BASE_TIME

OWNER = 188548043
GUEST = 777
STRANGER = 99
NOTICE_ID = 26854


class SpyAway:
    def __init__(self) -> None:
        self.intercepted: list[tuple[str, str]] = []

    async def intercept(self, message, *, kind, **_):
        self.intercepted.append((kind, message.text))
        return True


def chat(chat_id):
    return Chat(id=chat_id, type="private")


def person(user_id):
    return User(id=user_id, is_bot=False, first_name="Человек")


def notice(chat_id=OWNER):
    return Message(
        message_id=NOTICE_ID,
        date=BASE_TIME,
        chat=chat(chat_id),
        from_user=BOT_USER,
        text=ai_lines.DEVLAB_QUESTION_TEXT,
    )


def reply_update(update_id, text):
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id + 30000,
            date=BASE_TIME,
            chat=chat(OWNER),
            from_user=person(OWNER),
            text=text,
            reply_to_message=notice(),
        ),
    )


def button_update(update_id, data, user_id=OWNER, chat_id=OWNER):
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id=str(update_id),
            from_user=person(user_id),
            chat_instance="ci",
            data=data,
            message=notice(chat_id),
        ),
    )


@pytest.fixture
async def live(store, tmp_path):
    book = make_book(make_subscription(OWNER, "owner"), make_subscription(GUEST, "guest", "status"))
    notifier = FakeNotifier()
    gate = invites.Gatekeeper(
        InvitesConfig(), store, book, GuestStore(tmp_path / "guests.toml"), notifier
    )
    link = FakeNodeLink(
        results={protocol.ACTION_OWNER_POST: {protocol.FIELD_STATUS: protocol.STATUS_RUNNING}}
    )
    away = SpyAway()
    bot, session = recording_bot()
    workflow = {
        "store": store,
        "node_link": link,
        "config": Settings(),
        "notifier": notifier,
        "book": book,
        "gate": gate,
        "away": away,
        "active_ai_chats": object(),
        "tool_calls": object(),
        "devlab_notifier": FakeKicker(),
    }
    with prod_dispatcher(book, gate, store) as dp:

        async def feed(update):
            await dp.feed_update(bot, update, **workflow)

        yield feed, link, session, away
    await bot.session.close()


@pytest.mark.parametrize(
    ("code", "kind"),
    [("rel", protocol.MSG_RELEASE), ("stopc", protocol.MSG_STOP), ("res", protocol.MSG_RESUME)],
)
async def test_owner_buttons_reach_hub(live, code, kind):
    feed, link, session, _ = live
    await feed(button_update(1, f"dl:{code}:cgpkp2"))
    assert link.commands == [
        (protocol.ACTION_OWNER_POST, {protocol.FIELD_ID: "cgpkp2", protocol.FIELD_KIND: kind})
    ]
    toast = session.sent(AnswerCallbackQuery)[0].text
    assert toast == ai_lines.DEVLAB_POSTED_TOAST.format(status=protocol.STATUS_RUNNING)


async def test_stop_asks_confirmation_first(live):
    feed, link, session, _ = live
    await feed(button_update(1, "dl:stop:cgpkp2"))
    assert link.commands == []
    markup = session.sent(EditMessageReplyMarkup)[0].reply_markup
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["dl:stopc:cgpkp2", "dl:cnl:cgpkp2"]


@pytest.mark.parametrize(
    ("user_id", "chat_id"), [(STRANGER, OWNER), (GUEST, GUEST)], ids=["stranger", "guest"]
)
async def test_foreign_buttons_denied(live, user_id, chat_id):
    feed, link, session, _ = live
    await feed(button_update(1, "dl:rel:cgpkp2", user_id=user_id, chat_id=chat_id))
    assert link.commands == []
    answer = session.sent(AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (DENIED_ALERT_TEXT, True)


async def test_legacy_answer_button_is_silent(live):
    feed, link, session, _ = live
    await feed(button_update(1, "dl:ans:cgpkp2"))
    assert link.commands == []
    answer = session.sent(AnswerCallbackQuery)[0]
    assert (answer.text, answer.show_alert) == (None, None)
    assert session.sent(EditMessageReplyMarkup) == []


async def test_owner_reply_to_notice_goes_to_dialogue(live):
    feed, link, _, away = live
    await feed(reply_update(1, "go"))
    assert link.commands == []
    assert away.intercepted == [(KIND_TEXT, "go")]
