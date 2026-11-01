from __future__ import annotations

import pytest

from sa_home_bot.bot import ai_lines, devlab_notify
from sa_home_bot.bot.devlab_notify import DevlabNotifier
from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.proto.errors import ServiceUnavailableError
from tests.unit.support.factories import make_book, make_subscription
from tests.unit.support.fakes import FakeNodeLink, FakeNotifier

OWNER = 11
GUEST = 22


def job(kind=protocol.JOB_KIND_AGENT, status=protocol.STATUS_WAITING_OWNER, **extra):
    return {
        "id": "j1",
        "kind": kind,
        "project": "sa-home-bot",
        "model": "glm",
        "status": status,
        "tokens": 1200,
        "cost_usd": 0.0123,
        "title": "Починить <b>тест</b>",
        **extra,
    }


def message(seq, kind=protocol.MSG_QUESTION, payload=None, **job_args):
    return {
        "seq": seq,
        "job_id": "j1",
        "kind": kind,
        "payload": payload if payload is not None else {protocol.PAYLOAD_TEXT: "Какой <ключ>?"},
        "job": job(**job_args),
    }


def make(store, link, notifier=None, *, enabled=True):
    notifier = notifier or FakeNotifier()
    settings = Settings()
    settings.devlab_notify.enabled = enabled
    book = make_book(make_subscription(OWNER), make_subscription(GUEST, "guest", "status"))
    return (
        DevlabNotifier(
            get_node_link=lambda: link, store=store, config=settings, book=book, notifier=notifier
        ),
        notifier,
    )


def inbox(messages, cursor=None):
    last = messages[-1]["seq"] if messages else 0
    return {
        protocol.ACTION_OWNER_INBOX: {
            protocol.FIELD_MESSAGES: messages,
            protocol.FIELD_CURSOR: last if cursor is None else cursor,
        }
    }


def test_notifications_disabled_by_default():
    assert Settings().devlab_notify.enabled is False


async def test_first_run_stores_cursor_and_sends_nothing(store):
    link = FakeNodeLink(results=inbox([], cursor=57))
    notifier_obj, notifier = make(store, link)
    assert await notifier_obj.tick() == 0
    assert notifier.sent == []
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "57"
    assert link.commands[0][1][protocol.FIELD_SINCE] == -1


async def test_question_sent_to_admins_only_with_facts(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "5")
    link = FakeNodeLink(results=inbox([message(6)]))
    notifier_obj, notifier = make(store, link)
    assert await notifier_obj.tick() == 1
    assert [chat for chat, _ in notifier.sent] == [OWNER]
    text = notifier.sent[0][1]
    assert text.startswith(ai_lines.DEVLAB_QUESTION_TEXT)
    assert "sa-home-bot" in text and "agent/j1" in text and "glm" in text
    assert "1200" in text and "$0.0123" in text
    assert "Какой &lt;ключ&gt;?" in text and "Починить &lt;b&gt;тест&lt;/b&gt;" in text
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "6"
    assert link.commands[0][1][protocol.FIELD_SINCE] == 5


async def test_question_notice_remembers_message_id(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    link = FakeNodeLink(results=inbox([message(1)]))
    notifier_obj, notifier = make(store, link)
    await notifier_obj.tick()
    key = f"devlab_notice:{OWNER}:1001"
    assert await store.get_state(key) == "j1"


async def test_done_and_failed_have_no_notice_key(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    messages = [
        message(1, protocol.MSG_DONE, {protocol.PAYLOAD_SUMMARY: "Сделано"}, status="done"),
        message(2, protocol.MSG_FAILED, {protocol.PAYLOAD_TEXT: "Упал"}, status="failed"),
    ]
    notifier_obj, notifier = make(store, FakeNodeLink(results=inbox(messages)))
    assert await notifier_obj.tick() == 2
    assert notifier.sent[0][1].startswith(ai_lines.DEVLAB_DONE_TEXT)
    assert "Сделано" in notifier.sent[0][1]
    assert notifier.sent[1][1].startswith(ai_lines.DEVLAB_FAILED_TEXT)
    assert "Упал" in notifier.sent[1][1]
    assert await store.get_state(f"devlab_notice:{OWNER}:1001") is None


@pytest.mark.parametrize(
    "skipped",
    [
        message(1, kind=protocol.MSG_STARTED, payload={}),
        message(1, kind=protocol.MSG_STEP_DONE, payload={}),
        message(1, kind=protocol.MSG_STOPPED, payload={}, status="stopped"),
    ],
)
async def test_other_kinds_skipped_but_cursor_moves(store, skipped):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    notifier_obj, notifier = make(store, FakeNodeLink(results=inbox([skipped])))
    assert await notifier_obj.tick() == 0
    assert notifier.sent == []
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "1"


async def test_test_jobs_and_answered_question_skipped(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    messages = [
        message(1, protocol.MSG_DONE, {}, status="done"),
        message(2, status=protocol.STATUS_RUNNING),
    ]
    messages[0]["job"]["kind"] = protocol.JOB_KIND_TEST
    notifier_obj, notifier = make(store, FakeNodeLink(results=inbox(messages)))
    assert await notifier_obj.tick() == 0
    assert notifier.sent == []
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "2"


async def test_restart_does_not_resend(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    first, notifier = make(store, FakeNodeLink(results=inbox([message(1)])))
    await first.tick()
    second_link = FakeNodeLink(results=inbox([]))
    second, second_notifier = make(store, second_link)
    await second.tick()
    assert len(notifier.sent) == 1
    assert second_notifier.sent == []
    assert second_link.commands[0][1][protocol.FIELD_SINCE] == 1


async def test_hub_unavailable_keeps_cursor(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "4")
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    notifier_obj, notifier = make(store, link)
    assert await notifier_obj.tick() == 0
    assert notifier.sent == []
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "4"


async def test_unavailable_hub_before_first_run_keeps_no_cursor(store):
    link = FakeNodeLink(error=ServiceUnavailableError("нет связи"))
    notifier_obj, _ = make(store, link)
    await notifier_obj.tick()
    assert await store.get_state(devlab_notify.CURSOR_KEY) is None


async def test_failed_send_does_not_store_notice(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    link = FakeNodeLink(results=inbox([message(1)]))
    notifier_obj, _ = make(store, link, FakeNotifier(fail=True))
    await notifier_obj.tick()
    assert await store.get_state(f"devlab_notice:{OWNER}:1001") is None
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "1"


async def test_long_body_is_clipped(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    long = {protocol.PAYLOAD_TEXT: "я" * 5000}
    notifier_obj, notifier = make(store, FakeNodeLink(results=inbox([message(1, payload=long)])))
    await notifier_obj.tick()
    assert len(notifier.sent[0][1]) < 2000
