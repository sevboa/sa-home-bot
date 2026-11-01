from __future__ import annotations

import pytest

from sa_home_bot.bot import ai_lines
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
    FakeNodeLink,
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


async def press(cb, link, subscription=None, notifier=None):
    await handlers.on_devlab_callback(
        cb,
        config=Settings(),
        node_link=link,
        devlab_notifier=notifier,
        subscription=subscription or owner_sub(),
    )


@pytest.mark.parametrize("code", ["rel", "stop", "stopc", "res", "cnl"])
async def test_guest_is_denied_for_every_code(code):
    link = waiting_link()
    cb = callback(f"dl:{code}:j1", GUEST)
    await press(cb, link, guest_sub())
    assert cb.answered[0][0] == (DENIED_ALERT_TEXT,)
    assert link.calls == []
    assert cb.message.reply_markup_edits == []


async def test_missing_subscription_is_denied():
    link = waiting_link()
    cb = callback("dl:rel:j1")
    await handlers.on_devlab_callback(
        cb, config=Settings(), node_link=link, subscription=None
    )
    assert link.calls == []


@pytest.mark.parametrize(
    ("code", "kind"),
    [("rel", protocol.MSG_RELEASE), ("stopc", protocol.MSG_STOP), ("res", protocol.MSG_RESUME)],
)
async def test_post_codes(code, kind):
    link = waiting_link()
    notifier = FakeKicker()
    cb = callback(f"dl:{code}:j1")
    await press(cb, link, notifier=notifier)
    action, args = link.commands[0]
    assert action == protocol.ACTION_OWNER_POST
    assert args == {protocol.FIELD_ID: "j1", protocol.FIELD_KIND: kind}
    assert link.calls[0][2].service == protocol.SERVICE_NAME
    assert ai_lines.DEVLAB_POSTED_TOAST.format(status="running") in cb.answered[0][0]
    assert cb.message.reply_markup_edits == [None]
    assert notifier.kicked == 1


async def test_stop_needs_second_press():
    link = waiting_link()
    cb = callback("dl:stop:j1")
    await press(cb, link)
    assert link.calls == []
    markup = cb.message.reply_markup_edits[0]
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert data == ["dl:stopc:j1", "dl:cnl:j1"]
    cancel = callback("dl:cnl:j1")
    await press(cancel, link)
    restored = cancel.message.reply_markup_edits[0]
    assert "dl:rel:j1" in [b.callback_data for row in restored.inline_keyboard for b in row]
    assert link.calls == []


async def test_hub_refusal_toasts_and_clears_keyboard():
    link = FakeNodeLink(error=ProtoError(ERR_BAD_REQUEST, "нельзя из done"))
    cb = callback("dl:res:j1")
    await press(cb, link)
    assert "нельзя из done" in cb.answered[0][0][0]
    assert cb.message.reply_markup_edits == [None]


async def test_hub_down_keeps_keyboard():
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    cb = callback("dl:rel:j1")
    await press(cb, link)
    assert cb.answered[0][0] == (ai_lines.DEVLAB_HUB_DOWN_TEXT,)
    assert cb.message.reply_markup_edits == []


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



async def test_callback_without_message():
    link = waiting_link()
    cb = callback("dl:rel:j1")
    cb.message = None
    await press(cb, link)
    assert link.calls == []
    assert cb.answered[0][0] == ()


async def test_callback_from_other_user_in_owner_chat_is_denied():
    link = waiting_link()
    cb = FakeCallback("dl:rel:j1", chat_id=OWNER, from_user=FakeUser(id=99))
    await press(cb, link)
    assert cb.answered[0][0] == (DENIED_ALERT_TEXT,)
    assert link.calls == []
