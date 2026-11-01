from __future__ import annotations

import json
import time

import pytest

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.devlab_notify import NOTICE_KEY, DevlabNotifier, encode_entry
from sa_home_bot.bot.handlers import devlab as handlers
from sa_home_bot.bot.middlewares import DENIED_ALERT_TEXT
from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from tests.unit.support.factories import make_subscription
from tests.unit.support.fakes import (
    FakeCallback,
    FakeKicker,
    FakeMessage,
    FakeNodeLink,
    FakeSentMessage,
    FakeUser,
)

OWNER = 11
GUEST = 22


def owner_sub():
    return make_subscription(OWNER)


def guest_sub():
    return make_subscription(GUEST, "guest", "status")


def callback(data, user_id=OWNER):
    return FakeCallback(data, chat_id=user_id, from_user=FakeUser(id=user_id))


def waiting_link(status=protocol.STATUS_WAITING_OWNER):
    return FakeNodeLink(
        results={
            protocol.ACTION_OWNER_JOB: {protocol.FIELD_STATUS: status},
            protocol.ACTION_OWNER_POST: {protocol.FIELD_STATUS: protocol.STATUS_RUNNING},
        }
    )


async def press(cb, store, link, subscription=None, notifier=None):
    await handlers.on_devlab_callback(
        cb,
        config=Settings(),
        store=store,
        node_link=link,
        devlab_notifier=notifier,
        subscription=subscription or owner_sub(),
    )


@pytest.mark.parametrize("code", ["ans", "rel", "stop", "stopc", "res", "cnl"])
async def test_guest_is_denied_for_every_code(store, code):
    link = waiting_link()
    cb = callback(f"dl:{code}:j1", GUEST)
    await press(cb, store, link, guest_sub())
    assert cb.answered[0][0] == (DENIED_ALERT_TEXT,)
    assert link.calls == []
    assert cb.message.reply_markup_edits == []


async def test_missing_subscription_is_denied(store):
    link = waiting_link()
    cb = callback("dl:rel:j1")
    await handlers.on_devlab_callback(
        cb, config=Settings(), store=store, node_link=link, subscription=None
    )
    assert link.calls == []


@pytest.mark.parametrize(
    ("code", "kind"),
    [("rel", protocol.MSG_RELEASE), ("stopc", protocol.MSG_STOP), ("res", protocol.MSG_RESUME)],
)
async def test_post_codes(store, code, kind):
    link = waiting_link()
    notifier = FakeKicker()
    cb = callback(f"dl:{code}:j1")
    await press(cb, store, link, notifier=notifier)
    action, args = link.commands[0]
    assert action == protocol.ACTION_OWNER_POST
    assert args == {protocol.FIELD_ID: "j1", protocol.FIELD_KIND: kind}
    assert link.calls[0][2].service == protocol.SERVICE_NAME
    assert ai_lines.DEVLAB_POSTED_TOAST.format(status="running") in cb.answered[0][0]
    assert cb.message.reply_markup_edits == [None]
    assert notifier.kicked == 1


async def test_stop_needs_second_press(store):
    link = waiting_link()
    cb = callback("dl:stop:j1")
    await press(cb, store, link)
    assert link.calls == []
    markup = cb.message.reply_markup_edits[0]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["dl:stopc:j1", "dl:cnl:j1"]
    cancel = callback("dl:cnl:j1")
    await press(cancel, store, link)
    restored = cancel.message.reply_markup_edits[0]
    assert "dl:ans:j1" in [b.callback_data for row in restored.inline_keyboard for b in row]
    assert link.calls == []


async def test_hub_refusal_toasts_and_clears_keyboard(store):
    link = FakeNodeLink(error=ProtoError(ERR_BAD_REQUEST, "нельзя из done"))
    cb = callback("dl:res:j1")
    await press(cb, store, link)
    assert "нельзя из done" in cb.answered[0][0][0]
    assert cb.message.reply_markup_edits == [None]


async def test_hub_down_keeps_keyboard(store):
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    cb = callback("dl:rel:j1")
    await press(cb, store, link)
    assert cb.answered[0][0] == (ai_lines.DEVLAB_HUB_DOWN_TEXT,)
    assert cb.message.reply_markup_edits == []


async def test_answer_already_not_waiting(store):
    link = waiting_link(protocol.STATUS_RUNNING)
    cb = callback("dl:ans:j1")
    await press(cb, store, link)
    assert cb.answered[0][0] == (ai_lines.DEVLAB_ALREADY_TOAST.format(status="running"),)
    assert cb.message.answers == []
    assert await store.state_keys("devlab_reply:") == []


async def test_answer_sends_force_reply_and_stores_entry(store):
    cb = callback("dl:ans:j1")
    await press(cb, store, waiting_link())
    assert cb.message.answers == [ai_lines.DEVLAB_REPLY_PROMPT.format(job_id="j1")]
    assert type(cb.message.answer_markups[0]).__name__ == "ForceReply"
    keys = await store.state_keys("devlab_reply:")
    assert keys == [handlers.REPLY_KEY.format(chat_id=OWNER, message_id=1)]
    entry = json.loads(await store.get_state(keys[0]))
    assert entry["job_id"] == "j1"
    assert entry["user_id"] == OWNER


def reply_to(message_id, text, user_id=OWNER, chat_id=OWNER):
    msg = FakeMessage(chat_id=chat_id, text=text, user_id=user_id)
    msg.reply_to_message = FakeSentMessage(message_id=message_id)
    return msg


async def run_reply(msg, store, link, subscription=None, notifier=None):
    match = await handlers.DevlabReplyFilter()(
        msg, store=store, subscription=subscription or owner_sub()
    )
    if not match:
        return False
    await handlers.on_devlab_reply(
        msg,
        devlab_target=match["devlab_target"],
        config=Settings(),
        store=store,
        node_link=link,
        devlab_notifier=notifier,
        subscription=subscription or owner_sub(),
    )
    return True


async def test_reply_to_prompt_posts_answer_and_forgets(store):
    cb = callback("dl:ans:j1")
    link = waiting_link()
    await press(cb, store, link)
    notifier = FakeKicker()
    msg = reply_to(1, "  ключ 42  ")
    assert await run_reply(msg, store, link, notifier=notifier)
    action, args = link.commands[-1]
    assert action == protocol.ACTION_OWNER_POST
    assert args == {
        protocol.FIELD_ID: "j1",
        protocol.FIELD_KIND: protocol.MSG_ANSWER,
        protocol.PAYLOAD_TEXT: "ключ 42",
    }
    assert msg.answers == [ai_lines.DEVLAB_SENT_TEXT]
    assert len(await store.state_keys("devlab_reply:")) == 1
    assert notifier.kicked == 1


async def test_prompt_survives_restart(store):
    await press(callback("dl:ans:j1"), store, waiting_link())
    link = waiting_link()
    assert await run_reply(reply_to(1, "после рестарта"), store, link)
    assert link.commands[0][1][protocol.PAYLOAD_TEXT] == "после рестарта"


async def test_expired_prompt_not_forwarded(store):
    key = handlers.REPLY_KEY.format(chat_id=OWNER, message_id=5)
    entry = {"job_id": "j1", "user_id": OWNER, "created": time.time() - 90000}
    await store.set_state(key, json.dumps(entry))
    link = waiting_link()
    msg = reply_to(5, "поздно")
    assert await run_reply(msg, store, link)
    assert msg.answers == [ai_lines.DEVLAB_PROMPT_EXPIRED_TEXT]
    assert link.calls == []
    assert await store.get_state(key) is None


async def test_reply_to_question_notice(store):
    await store.set_state(NOTICE_KEY.format(chat_id=OWNER, message_id=77), encode_entry("j9"))
    link = waiting_link()
    msg = reply_to(77, "обычный реплай")
    assert await run_reply(msg, store, link)
    assert link.commands[0][1][protocol.FIELD_ID] == "j9"
    assert msg.answers == [ai_lines.DEVLAB_SENT_TEXT]


async def test_hub_refusal_on_reply_reports_and_keeps_key(store):
    key = NOTICE_KEY.format(chat_id=OWNER, message_id=77)
    await store.set_state(key, encode_entry("j9"))
    link = FakeNodeLink(error=ProtoError(ERR_BAD_REQUEST, "уже не ждёт"))
    msg = reply_to(77, "ответ")
    assert await run_reply(msg, store, link)
    assert "уже не ждёт" in msg.answers[0]
    assert await store.get_state(key) is not None


async def test_guest_reply_goes_on(store):
    await store.set_state(NOTICE_KEY.format(chat_id=GUEST, message_id=77), encode_entry("j9"))
    msg = reply_to(77, "привет", user_id=GUEST, chat_id=GUEST)
    assert not await run_reply(msg, store, waiting_link(), guest_sub())


async def test_other_prompt_user_and_commands_go_on(store):
    await press(callback("dl:ans:j1"), store, waiting_link())
    assert not await run_reply(reply_to(1, "чужой", user_id=99), store, waiting_link())
    assert not await run_reply(reply_to(1, "/start"), store, waiting_link())
    assert not await run_reply(reply_to(1, "  "), store, waiting_link())
    assert not await run_reply(reply_to(555, "реплай Альфреду"), store, waiting_link())
    no_reply = FakeMessage(chat_id=OWNER, text="просто текст", user_id=OWNER)
    no_reply.reply_to_message = None
    assert not await run_reply(no_reply, store, waiting_link())


def test_router_included_before_ai():
    import inspect

    from sa_home_bot.bot import setup

    order = [
        line.split("(")[1].split(".")[0]
        for line in inspect.getsource(setup.build_dispatcher).splitlines()
        if "include_router(" in line
    ]
    assert order.index("devlab") == order.index("away") + 1
    assert order.index("devlab") < order.index("ai")



async def test_callback_without_message(store):
    link = waiting_link()
    cb = callback("dl:rel:j1")
    cb.message = None
    await press(cb, store, link)
    assert link.calls == []
    assert cb.answered[0][0] == ()


async def test_callback_from_other_user_in_owner_chat_is_denied(store):
    link = waiting_link()
    cb = FakeCallback("dl:rel:j1", chat_id=OWNER, from_user=FakeUser(id=99))
    await press(cb, store, link)
    assert cb.answered[0][0] == (DENIED_ALERT_TEXT,)
    assert link.calls == []


async def test_reply_from_other_user_in_owner_chat_goes_on(store):
    await store.set_state(NOTICE_KEY.format(chat_id=OWNER, message_id=77), encode_entry("j9"))
    msg = reply_to(77, "чужой", user_id=99, chat_id=OWNER)
    assert not await run_reply(msg, store, waiting_link())


async def test_answer_with_hub_down_creates_no_key(store):
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    cb = callback("dl:ans:j1")
    await press(cb, store, link)
    assert cb.answered[0][0] == (ai_lines.DEVLAB_HUB_DOWN_TEXT,)
    assert cb.message.answers == []
    assert await store.state_keys("devlab_reply:") == []


async def test_reply_with_hub_down_keeps_key(store):
    key = NOTICE_KEY.format(chat_id=OWNER, message_id=77)
    await store.set_state(key, encode_entry("j9"))
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    msg = reply_to(77, "ответ")
    assert await run_reply(msg, store, link)
    assert msg.answers == [ai_lines.DEVLAB_HUB_DOWN_TEXT]
    assert await store.get_state(key) is not None


async def test_repeat_reply_after_answer_is_refused_by_hub_not_dialogue(store):
    key = NOTICE_KEY.format(chat_id=OWNER, message_id=77)
    await store.set_state(key, encode_entry("j9"))
    assert await run_reply(reply_to(77, "первый"), store, waiting_link())
    link = FakeNodeLink(error=ProtoError(ERR_BAD_REQUEST, "не ждёт ответа"))
    msg = reply_to(77, "второй")
    assert await run_reply(msg, store, link)
    assert "не ждёт ответа" in msg.answers[0]


async def test_expired_notice_not_forwarded_and_removed(store):
    key = NOTICE_KEY.format(chat_id=OWNER, message_id=77)
    old = json.dumps({"job_id": "j9", "created": time.time() - 8 * 86400})
    await store.set_state(key, old)
    link = waiting_link()
    msg = reply_to(77, "поздно")
    assert await run_reply(msg, store, link)
    assert msg.answers == [ai_lines.DEVLAB_PROMPT_EXPIRED_TEXT]
    assert link.calls == []
    assert await store.get_state(key) is None


async def test_tick_purges_expired_keys_only(store):
    fresh = NOTICE_KEY.format(chat_id=OWNER, message_id=1)
    stale = NOTICE_KEY.format(chat_id=OWNER, message_id=2)
    stale_reply = handlers.REPLY_KEY.format(chat_id=OWNER, message_id=3)
    fresh_reply = handlers.REPLY_KEY.format(chat_id=OWNER, message_id=4)
    await store.set_state(fresh, encode_entry("a"))
    await store.set_state(stale, json.dumps({"job_id": "a", "created": time.time() - 8 * 86400}))
    await store.set_state(stale_reply, json.dumps({"job_id": "a", "created": time.time() - 90000}))
    await store.set_state(fresh_reply, json.dumps({"job_id": "a", "created": time.time() - 3600}))
    notifier = DevlabNotifier(
        get_node_link=lambda: None,
        store=store,
        config=Settings(),
        book=None,
        notifier=None,
    )
    await notifier.tick()
    assert await store.get_state(fresh) is not None
    assert await store.get_state(stale) is None
    assert await store.get_state(stale_reply) is None
    assert await store.get_state(fresh_reply) is not None
