from __future__ import annotations

import pytest

from sa_home_bot.bot import ai_lines
from sa_home_bot.bot.handlers import devlab as handlers
from sa_home_bot.bot.middlewares import DENIED_ALERT_TEXT, DENIED_TEXT
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


def jobs_link(jobs, total, offset=0):
    return FakeNodeLink(
        results={
            protocol.ACTION_OWNER_JOBS: {
                protocol.FIELD_JOBS: jobs,
                protocol.FIELD_TOTAL: total,
                protocol.FIELD_OFFSET: offset,
            },
            protocol.ACTION_OWNER_JOB: {
                protocol.FIELD_JOB: {
                    protocol.FIELD_ID: "job-1",
                    protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
                    protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
                    protocol.FIELD_TITLE: "Test Job",
                    protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
                    "project": "proj",
                    "created": "2026-11-01T12:00:00",
                    "started": "2026-11-01T12:01:00",
                    "executor": "exec-1",
                    protocol.PAYLOAD_TOKENS: 1000,
                }
            },
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


@pytest.mark.parametrize("code", ["rel", "stop", "stopc", "res", "cnl", "stopw", "cnlw"])
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


async def test_stalled_stop_confirm_and_cancel_keep_stop_button():
    link = waiting_link()
    cb = callback("dl:stopw:j1")
    await press(cb, link)
    assert link.calls == []
    data = [
        b.callback_data for row in cb.message.reply_markup_edits[0].inline_keyboard for b in row
    ]
    assert data == ["dl:stopc:j1", "dl:cnlw:j1"]
    cancel = callback("dl:cnlw:j1")
    await press(cancel, link)
    restored = cancel.message.reply_markup_edits[0]
    assert [b.callback_data for row in restored.inline_keyboard for b in row] == ["dl:stopw:j1"]
    assert link.calls == []


async def test_cmd_jobs_owner_gets_rich_page():
    from sa_home_bot.bot.devlab_jobs_view import PAGE_SIZE

    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Job 1",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = jobs_link(jobs, 1)
    msg = FakeMessage(chat_id=OWNER, user_id=OWNER, text="/jobs")
    await handlers.cmd_jobs(
        msg, config=Settings(), node_link=link, subscription=owner_sub()
    )
    assert msg.rich_answers
    assert msg.rich_answers[0][0] is not None
    assert msg.rich_answers[0][1] is not None
    assert link.commands[0][0] == protocol.ACTION_OWNER_JOBS
    assert link.commands[0][1] == {protocol.FIELD_OFFSET: 0, protocol.FIELD_LIMIT: PAGE_SIZE}


async def test_cmd_jobs_guest_denied():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Job 1",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = jobs_link(jobs, 1)
    msg = FakeMessage(chat_id=OWNER, user_id=GUEST, text="/jobs")
    await handlers.cmd_jobs(
        msg, config=Settings(), node_link=link, subscription=guest_sub()
    )
    assert msg.answers[0] == DENIED_TEXT
    assert link.calls == []


async def test_cmd_jobs_hub_down():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Job 1",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = FakeNodeLink(
        error=ServiceUnavailableError("нет связи"),
        results={protocol.ACTION_OWNER_JOBS: {protocol.FIELD_JOBS: jobs, protocol.FIELD_TOTAL: 1}},
    )
    msg = FakeMessage(chat_id=OWNER, user_id=OWNER, text="/jobs")
    await handlers.cmd_jobs(
        msg, config=Settings(), node_link=link, subscription=owner_sub()
    )
    assert ai_lines.DEVLAB_HUB_DOWN_TEXT in msg.answers[0]


async def press_jobs(cb, link, subscription=None):
    await handlers.on_jobs_callback(
        cb, config=Settings(), node_link=link, subscription=subscription or owner_sub()
    )


async def test_jobs_callback_guest_denied():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Job 1",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = jobs_link(jobs, 1)
    cb = callback("dj:p:0", GUEST)
    await press_jobs(cb, link, guest_sub())
    assert cb.answered[0][0] == (DENIED_ALERT_TEXT,)
    assert cb.answered[0][1].get("show_alert") is True
    assert link.calls == []


async def test_jobs_callback_page_navigation():
    jobs = [
        {
            protocol.FIELD_ID: f"job-{i}",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: f"Job {i}",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
        for i in range(25)
    ]
    link = jobs_link(jobs, 25)
    cb = callback("dj:p:20")
    await press_jobs(cb, link)
    assert link.commands[0][1] == {protocol.FIELD_OFFSET: 20, protocol.FIELD_LIMIT: 20}
    assert cb.message.edit_calls
    rich_msg = cb.message.edit_calls[0][0][0]
    assert rich_msg is not None
    assert "21." in rich_msg.html
    assert "40." not in rich_msg.html


async def test_jobs_callback_page_clamp_offset():
    jobs = [
        {
            protocol.FIELD_ID: f"job-{i}",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: f"Job {i}",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
        for i in range(5)
    ]
    link = FakeNodeLink(
        results={
            protocol.ACTION_OWNER_JOBS: {
                protocol.FIELD_JOBS: jobs,
                protocol.FIELD_TOTAL: 5,
                protocol.FIELD_OFFSET: 0,
            },
            protocol.ACTION_OWNER_JOB: {
                protocol.FIELD_JOB: {
                    protocol.FIELD_ID: "job-1",
                    protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
                    protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
                    protocol.FIELD_TITLE: "Test Job",
                    protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
                    "project": "proj",
                    "created": "2026-11-01T12:00:00",
                    "started": "2026-11-01T12:01:00",
                    "executor": "exec-1",
                    protocol.PAYLOAD_TOKENS: 1000,
                }
            },
            protocol.ACTION_OWNER_POST: {protocol.FIELD_STATUS: protocol.STATUS_RUNNING},
        }
    )

    async def tracking_command(action, args=None, dst=None, *, timeout=None):
        link.calls.append((action, args or {}, dst))
        if action == protocol.ACTION_OWNER_JOBS:
            offset = args.get(protocol.FIELD_OFFSET, 0) if args else 0
            if offset == 100:
                return {
                    protocol.FIELD_JOBS: [],
                    protocol.FIELD_TOTAL: 5,
                    protocol.FIELD_OFFSET: 100,
                }
            else:
                return {
                    protocol.FIELD_JOBS: jobs,
                    protocol.FIELD_TOTAL: 5,
                    protocol.FIELD_OFFSET: 0,
                }
        return link.results.get(action, link.result)

    link.command = tracking_command

    cb = callback("dj:p:100")
    await press_jobs(cb, link)
    assert link.commands[0][1] == {protocol.FIELD_OFFSET: 100, protocol.FIELD_LIMIT: 20}
    assert link.commands[1][1] == {protocol.FIELD_OFFSET: 0, protocol.FIELD_LIMIT: 20}
    rich_msg = cb.message.edit_calls[0][0][0]
    assert rich_msg is not None
    assert "стр. 1 из 1" in rich_msg.html


async def test_jobs_callback_card_and_back():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Test Job",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = jobs_link(jobs, 1)
    cb = callback("dj:c:job-1:0")
    await press_jobs(cb, link)
    assert link.commands[0][0] == protocol.ACTION_OWNER_JOB
    assert link.commands[0][1] == {protocol.FIELD_ID: "job-1"}
    assert cb.message.edit_calls
    rich_msg = cb.message.edit_calls[0][0][0]
    assert rich_msg is not None
    assert "job-1" in rich_msg.html
    assert "Test Job" in rich_msg.html
    markup = cb.message.edit_calls[0][1].get("reply_markup")
    data_values = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "dj:p:0" in data_values
    assert "dj:c:job-1:0" in data_values

    cb2 = callback("dj:p:0")
    await press_jobs(cb2, link)
    assert link.commands[1][0] == protocol.ACTION_OWNER_JOBS
    assert link.commands[1][1] == {protocol.FIELD_OFFSET: 0, protocol.FIELD_LIMIT: 20}
    rich_msg2 = cb2.message.edit_calls[0][0][0]
    assert rich_msg2 is not None
    assert "Задания devlab" in rich_msg2.html


async def test_jobs_callback_home_button():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Test Job",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = jobs_link(jobs, 1)
    cb = callback("dj:c:job-1:20")
    await press_jobs(cb, link)
    markup = cb.message.edit_calls[0][1].get("reply_markup")
    data_values = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "dj:p:0" in data_values
    assert "dj:p:20" in data_values


async def test_jobs_callback_hub_error_on_page():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Job 1",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = FakeNodeLink(
        error=ProtoError(ERR_BAD_REQUEST, "bad offset"),
        results={protocol.ACTION_OWNER_JOBS: {protocol.FIELD_JOBS: jobs, protocol.FIELD_TOTAL: 1}},
    )
    cb = callback("dj:p:0")
    await press_jobs(cb, link)
    assert "bad offset" in cb.answered[0][0][0]
    assert cb.answered[0][1].get("show_alert") is True
    assert cb.message.edit_calls == []


async def test_jobs_callback_hub_error_on_card():
    link = FakeNodeLink(
        error=ProtoError(ERR_BAD_REQUEST, "not found"),
        results={protocol.ACTION_OWNER_JOB: {}},
    )
    cb = callback("dj:c:job-1:0")
    await press_jobs(cb, link)
    assert "not found" in cb.answered[0][0][0]
    assert cb.answered[0][1].get("show_alert") is True
    assert cb.message.edit_calls == []


async def test_jobs_callback_hub_down():
    jobs = [
        {
            protocol.FIELD_ID: "job-1",
            protocol.FIELD_KIND: protocol.JOB_KIND_AGENT,
            protocol.FIELD_STATUS: protocol.STATUS_RUNNING,
            protocol.FIELD_TITLE: "Job 1",
            protocol.FIELD_PAYLOAD: {protocol.PAYLOAD_MODEL: "gpt-4o"},
        }
    ]
    link = FakeNodeLink(
        error=ServiceUnavailableError("down"),
        results={protocol.ACTION_OWNER_JOBS: {protocol.FIELD_JOBS: jobs, protocol.FIELD_TOTAL: 1}},
    )
    cb = callback("dj:p:0")
    await press_jobs(cb, link)
    assert cb.answered[0][0] == (ai_lines.DEVLAB_HUB_DOWN_TEXT,)
    assert cb.answered[0][1].get("show_alert") is True
    assert cb.message.edit_calls == []
