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


async def test_answer_hint_only_in_question(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    messages = [
        message(1),
        message(2, protocol.MSG_DONE, {protocol.PAYLOAD_SUMMARY: "Сделано"}, status="done"),
    ]
    notifier_obj, notifier = make(store, FakeNodeLink(results=inbox(messages)))
    await notifier_obj.tick()
    assert notifier.sent[0][1].endswith(ai_lines.DEVLAB_ANSWER_HINT)
    assert ai_lines.DEVLAB_ANSWER_HINT not in notifier.sent[1][1]


async def test_purge_legacy_reply_keys(store):
    legacy = [f"devlab_notice:{OWNER}:1", f"devlab_reply:{OWNER}:2"]
    for key in legacy:
        await store.set_state(key, '{"job_id": "j1"}')
    await store.set_state(devlab_notify.CURSOR_KEY, "7")
    notifier_obj, _ = make(store, None)
    await notifier_obj.purge_legacy_keys()
    for key in legacy:
        assert await store.get_state(key) is None
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "7"


async def test_done_and_failed_texts(store):
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


async def test_failed_send_still_moves_cursor(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    link = FakeNodeLink(results=inbox([message(1)]))
    notifier_obj, _ = make(store, link, FakeNotifier(fail=True))
    await notifier_obj.tick()
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "1"


async def test_long_body_is_clipped(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    long = {protocol.PAYLOAD_TEXT: "я" * 5000}
    notifier_obj, notifier = make(store, FakeNodeLink(results=inbox([message(1, payload=long)])))
    await notifier_obj.tick()
    assert len(notifier.sent[0][1]) < 2000


async def test_every_admin_gets_own_notice_and_guest_none(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    link = FakeNodeLink(results=inbox([message(1)]))
    notifier = FakeNotifier()
    settings = Settings()
    book = make_book(
        make_subscription(OWNER),
        make_subscription(33, "second"),
        make_subscription(GUEST, "guest", "status"),
    )
    notifier_obj = DevlabNotifier(
        get_node_link=lambda: link, store=store, config=settings, book=book, notifier=notifier
    )
    await notifier_obj.tick()
    assert [chat for chat, _ in notifier.sent] == [OWNER, 33]


@pytest.mark.parametrize(
    ("kind", "codes"),
    [
        (protocol.MSG_QUESTION, ["rel", "stop"]),
        (protocol.MSG_FAILED, ["res"]),
        (protocol.MSG_STALLED, ["stopw"]),
        (protocol.MSG_DONE, None),
    ],
)
def test_notice_keyboard_by_kind(kind, codes):
    keyboard = devlab_notify.notice_keyboard(kind, "j1")
    if codes is None:
        assert keyboard is None
        return
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == [f"dl:{code}:j1" for code in codes]


def epic_message(seq=6, epic="ab12cd"):
    return message(
        seq,
        protocol.MSG_EPIC_WAITING,
        payload={protocol.PAYLOAD_TEXT: "Эпик встал <тут>", protocol.PARAM_EPIC: epic},
        status=protocol.STATUS_FAILED,
    )


def test_epic_notice_keyboard_resume_and_stop_by_epic_id():
    keyboard = devlab_notify.notice_keyboard(protocol.MSG_EPIC_WAITING, "j1", "ab12cd")
    rows = [(b.text, b.callback_data) for row in keyboard.inline_keyboard for b in row]
    assert rows == [
        (ai_lines.DEVLAB_BTN_EPIC_RESUME, "dl:epr:ab12cd"),
        (ai_lines.DEVLAB_BTN_EPIC_STOP, "dl:eps:ab12cd"),
    ]
    assert devlab_notify.notice_keyboard(protocol.MSG_EPIC_WAITING, "j1") is None


async def test_epic_waiting_is_delivered_with_epic_buttons(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "5")
    link = FakeNodeLink(results=inbox([epic_message()]))
    notifier_obj, notifier = make(store, link)
    assert await notifier_obj.tick() == 1
    text = notifier.sent[0][1]
    assert text.startswith(ai_lines.DEVLAB_EPIC_WAITING_TEXT)
    assert "Эпик: ab12cd" in text
    assert "Эпик встал &lt;тут&gt;" in text
    data = [b.callback_data for row in notifier.markups[0].inline_keyboard for b in row]
    assert data == ["dl:epr:ab12cd", "dl:eps:ab12cd"]


async def test_epic_waiting_without_epic_id_is_skipped(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "5")
    link = FakeNodeLink(results=inbox([epic_message(epic="")]))
    notifier_obj, notifier = make(store, link)
    assert await notifier_obj.tick() == 0
    assert notifier.sent == []


async def test_question_sent_with_buttons(store):
    await store.set_state(devlab_notify.CURSOR_KEY, "5")
    link = FakeNodeLink(results=inbox([message(6)]))
    notifier_obj, notifier = make(store, link)
    await notifier_obj.tick()
    assert notifier.markups[0] is not None


@pytest.mark.parametrize(
    "kind",
    [protocol.MSG_QUESTION, protocol.MSG_DONE, protocol.MSG_FAILED, protocol.MSG_STALLED],
)
async def test_delivery_uses_stub_lead_without_llm(store, kind):
    status = (
        protocol.STATUS_RUNNING if kind == protocol.MSG_STALLED else protocol.STATUS_WAITING_OWNER
    )
    link = FakeNodeLink(results=inbox([message(6, kind, status=status)]))
    notifier_obj, notifier = make(store, link)
    await store.set_state(devlab_notify.CURSOR_KEY, "5")
    await notifier_obj.tick()
    assert notifier.sent
    text = notifier.sent[0][1]
    assert text.startswith(devlab_notify.LEADS[kind])
    assert [action for action, _, _ in link.calls] == [protocol.ACTION_OWNER_INBOX]


STALLED_PAYLOAD = {protocol.PAYLOAD_SILENT_MIN: 42, protocol.PAYLOAD_EXECUTOR_ALIVE: False}


async def test_stalled_notice_text_and_stop_button(store):
    msg = message(1, protocol.MSG_STALLED, STALLED_PAYLOAD, status=protocol.STATUS_RUNNING)
    link = FakeNodeLink(results=inbox([msg]))
    notifier_obj, notifier = make(store, link)
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    assert await notifier_obj.tick() == 1
    text = notifier.sent[0][1]
    assert text.startswith(ai_lines.DEVLAB_STALLED_TEXT)
    assert "Без вестей: 42 мин" in text
    assert "Исполнитель: не отвечает" in text
    assert "glm" in text
    keyboard = notifier.markups[0]
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert data == ["dl:stopw:j1"]


async def test_stalled_skipped_when_job_no_longer_running(store):
    msg = message(1, protocol.MSG_STALLED, STALLED_PAYLOAD, status=protocol.STATUS_STOPPED)
    link = FakeNodeLink(results=inbox([msg]))
    notifier_obj, notifier = make(store, link)
    await store.set_state(devlab_notify.CURSOR_KEY, "0")
    assert await notifier_obj.tick() == 0
    assert notifier.sent == []
    assert await store.get_state(devlab_notify.CURSOR_KEY) == "1"
