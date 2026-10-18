from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sa_home_bot.bot import tools
from sa_home_bot.bot.tools import remind
from sa_home_bot.config import (
    LlmConfig,
    Settings,
)
from sa_home_bot.llm import prompt
from sa_home_bot.proto.addresses import LLM_NODE, LLM_SERVICE
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.tasks import protocol as task_protocol
from tests.unit.tools_support import (
    CHAT_ID,
    FakeNodeLink,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)


def test_close_pending_tool_calls_returns_unchanged_when_last_is_not_tool_calls():
    history = [{"role": "user", "content": "привет"}]
    assert remind._close_pending_tool_calls(history) == history


@pytest.mark.parametrize(
    ("self_result", "expected_content"),
    [(None, "(результат не сохранён)"), ("Обновление поставлено", "Обновление поставлено")],
    ids=["placeholder", "self-result"],
)
def test_close_pending_tool_calls_fills_the_dangling_call(self_result, expected_content):
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [{"function": {"name": "node_manage", "arguments": {}}}],
        },
    ]
    snapshot = remind._close_pending_tool_calls(history, self_result=self_result)
    assert snapshot[-1] == {"role": "tool", "content": expected_content, "name": "node_manage"}
    assert history[-1].get("role") != "tool"


def test_close_pending_tool_calls_self_result_only_for_first_of_several():
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "node_manage", "arguments": {"node": "arch-t480"}}},
                {"function": {"name": "node_manage", "arguments": {"node": "jeeves"}}},
            ],
        },
    ]
    snapshot = remind._close_pending_tool_calls(history, self_result="готово для arch-t480")
    tail = snapshot[-2:]
    assert tail[0] == {"role": "tool", "content": "готово для arch-t480", "name": "node_manage"}
    assert tail[1] == {"role": "tool", "content": "(результат не сохранён)", "name": "node_manage"}


def test_close_pending_tool_calls_skips_already_recorded_pending_calls():
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "node_manage", "arguments": {"node": "arch-t480"}}},
                {"function": {"name": "node_manage", "arguments": {"node": "jeeves"}}},
            ],
        },
        {"role": "tool", "content": "уже записанный результат arch-t480", "name": "node_manage"},
    ]
    snapshot = remind._close_pending_tool_calls(history, self_result="готово для jeeves")
    assert snapshot[-1] == {"role": "tool", "content": "готово для jeeves", "name": "node_manage"}
    assert snapshot[-2] == {
        "role": "tool",
        "content": "уже записанный результат arch-t480",
        "name": "node_manage",
    }


async def test_remind_creates_task_for_future_time():
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(node_link=link), {"when": when, "text": "полить цветы"})
    assert "задача поставлена" in result
    assert len(link.calls) == 1
    action, args, dst = link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    assert dst.node == task_protocol.NODE_ID
    assert dst.service == task_protocol.SERVICE_NAME
    assert args["dst_node"] == LLM_NODE
    assert args["dst_service"] == LLM_SERVICE
    assert args["action"] == task_protocol.ACTION_CHAT_LOOP
    assert args["meta"]["kind"] == task_protocol.TASK_KIND_LLM_CHAT
    assert args["meta"]["chat_id"] == CHAT_ID
    assert "полить цветы" in args["args"]["messages"][-1]["content"]


async def test_remind_carries_message_thread_id_in_meta():
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(node_link=link, message_thread_id=42),
        {"when": when, "text": "полить цветы"},
    )
    _action, args, _dst = link.calls[0]
    assert args["meta"]["message_thread_id"] == 42


async def test_remind_includes_history_snapshot_in_directive():
    link = FakeNodeLink()
    history = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "привет!"}]
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(node_link=link, history=history), {"when": when, "text": "напомни"}
    )
    messages = link.calls[0][1]["args"]["messages"]
    assert messages[:2] == history


async def test_remind_closes_dangling_tool_calls_in_snapshot():
    link = FakeNodeLink()
    history = [
        {"role": "user", "content": "напомни и глянь погоду"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "remind", "arguments": {}}},
                {"function": {"name": "get_weather", "arguments": {}}},
            ],
        },
    ]
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await tools.tool_remind(
        _ctx(node_link=link, history=history), {"when": when, "text": "напомни"}
    )
    messages = link.calls[0][1]["args"]["messages"]
    assert messages[2] == {"role": "tool", "content": "(результат не сохранён)", "name": "remind"}
    assert messages[3] == {
        "role": "tool",
        "content": "(результат не сохранён)",
        "name": "get_weather",
    }
    assert messages[4]["role"] == prompt._DIRECTIVE_ROLE
    assert messages[4]["content"].startswith(prompt._DIRECTIVE_MARKER)
    assert history == [
        {"role": "user", "content": "напомни и глянь погоду"},
        {
            "role": "assistant",
            "tool_calls": [
                {"function": {"name": "remind", "arguments": {}}},
                {"function": {"name": "get_weather", "arguments": {}}},
            ],
        },
    ]


def FUTURE() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


@pytest.mark.parametrize(
    "args",
    [
        {"when": (datetime.now(UTC) - timedelta(hours=1)).isoformat(), "text": "поздно"},
        {"when": "завтра", "text": "текст"},
        {"when": FUTURE(), "text": ""},
        {"after_event": {"node": "arch-t480", "event": "something_else"}, "text": "x"},
        {"after_event": {"event": "restart_applied"}, "text": "x"},
    ],
    ids=["past-time", "invalid-iso", "missing-text", "unknown-event", "missing-node"],
)
async def test_remind_rejects(args):
    link = FakeNodeLink()
    result = await tools.tool_remind(_ctx(node_link=link), args)
    assert result.startswith("ошибка")
    assert link.calls == []


async def test_remind_without_chat_id():
    result = await tools.tool_remind(
        _ctx(chat_id=None, node_link=FakeNodeLink()), {"when": "x", "text": "x"}
    )
    assert "вне диалога" in result


async def test_remind_without_node_link():
    result = await tools.tool_remind(_ctx(node_link=None), {"when": FUTURE(), "text": "x"})
    assert "служба задач недоступна" in result


async def test_remind_reports_error_when_task_service_unreachable():
    link = FakeNodeLink(raises=ServiceUnavailableError("boom"))
    result = await tools.tool_remind(_ctx(node_link=link), {"when": FUTURE(), "text": "x"})
    assert result.startswith("внутренняя ошибка")


async def test_remind_after_event_creates_task_without_when():
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(node_link=link),
        {
            "after_event": {"node": "arch-t480", "event": "restart_applied"},
            "text": "продолжи обновление роя",
        },
    )
    assert "жду событие" in result
    assert "arch-t480" in result
    action, args, dst = link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    due_at = datetime.fromisoformat(args["due_at"])
    assert due_at > datetime.now(UTC)
    directive = args["args"]["messages"][-1]["content"]
    assert due_at.strftime("%H:%M") not in directive
    assert "продолжи обновление роя" in args["args"]["messages"][-1]["content"]


async def test_remind_after_event_puts_await_event_in_create_rpc():
    link = FakeNodeLink()
    await tools.tool_remind(
        _ctx(node_link=link),
        {
            "after_event": {"node": "arch-t480", "event": "restart_applied"},
            "text": "продолжи",
        },
    )
    _action, args, _dst = link.calls[0]
    assert args["await_event"] == {"node": "arch-t480", "event_type": "restart_applied"}


async def test_remind_after_event_stores_awaited_pair_in_meta():
    link = FakeNodeLink()
    await tools.tool_remind(
        _ctx(node_link=link),
        {"after_event": {"node": "arch-t480", "event": "restart_applied"}, "text": "продолжи"},
    )
    _action, args, _dst = link.calls[0]
    assert args["meta"]["awaited_node"] == "arch-t480"
    assert args["meta"]["awaited_event"] == "restart_applied"


async def test_remind_after_event_rejects_same_event_that_woke_this_run():
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(node_link=link, woken_by=("jeeves", "update_finished")),
        {"after_event": {"node": "jeeves", "event": "update_finished"}, "text": "x"},
    )
    assert result.startswith("ошибка")
    assert "уже наступило" in result
    assert link.calls == []


async def test_remind_after_event_allows_different_node_than_woke_this_run():
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(node_link=link, woken_by=("jeeves", "update_finished")),
        {"after_event": {"node": "arch-t480", "event": "update_finished"}, "text": "x"},
    )
    assert "жду событие" in result
    assert len(link.calls) == 1


async def test_remind_after_event_allows_different_event_than_woke_this_run():
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(node_link=link, woken_by=("jeeves", "update_finished")),
        {"after_event": {"node": "jeeves", "event": "restart_applied"}, "text": "x"},
    )
    assert "жду событие" in result
    assert len(link.calls) == 1


async def test_remind_after_event_works_without_store():
    link = FakeNodeLink()
    result = await tools.tool_remind(
        _ctx(None, node_link=link),
        {"after_event": {"node": "arch-t480", "event": "restart_applied"}, "text": "x"},
    )
    assert "жду событие" in result
    _action, args, _dst = link.calls[0]
    assert args["await_event"] == {"node": "arch-t480", "event_type": "restart_applied"}


async def test_remind_sends_reason_not_think():
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    settings = Settings(llm=LlmConfig(think_chat=True))
    await tools.tool_remind(_ctx(settings=settings, node_link=link), {"when": when, "text": "x"})
    args = link.calls[0][1]["args"]
    assert args["reason"] == "high"
    assert "think" not in args


@pytest.mark.parametrize(
    "llm",
    [LlmConfig(think_chat=False), LlmConfig(mode="single_call", single_call_think=False)],
    ids=["think-chat-disabled", "single-call-think-false"],
)
async def test_remind_reason_is_off(llm):
    link = FakeNodeLink()
    await tools.tool_remind(
        _ctx(settings=Settings(llm=llm), node_link=link), {"when": FUTURE(), "text": "x"}
    )
    assert link.calls[0][1]["args"]["reason"] == "off"


async def test_remind_when_still_works_without_after_event():
    link = FakeNodeLink()
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    result = await tools.tool_remind(_ctx(node_link=link), {"when": when, "text": "полить цветы"})
    assert "задача поставлена" in result
    assert "await_event" not in link.calls[0][1]


async def test_schedule_agent_dialogue_creates_chat_loop_task_without_dialogue_id():
    link = FakeNodeLink()
    messages = [{"role": "user", "content": "скажи гостю B привет от A"}]
    other_chat_id = 222
    result = await tools.schedule_agent_dialogue(link, other_chat_id, messages, "high", 60.0)
    assert result == {"task_id": 1}
    assert len(link.calls) == 1
    action, args, dst = link.calls[0]
    assert action == task_protocol.ACTION_CREATE
    assert dst.node == task_protocol.NODE_ID
    assert dst.service == task_protocol.SERVICE_NAME
    assert args["dst_node"] == LLM_NODE
    assert args["dst_service"] == LLM_SERVICE
    assert args["action"] == task_protocol.ACTION_CHAT_LOOP
    assert args["args"] == {"messages": messages, "reason": "high", "chat_id": other_chat_id}
    assert args["timeout_s"] == 60.0
    assert args["meta"] == {
        "kind": task_protocol.TASK_KIND_LLM_CHAT,
        "chat_id": other_chat_id,
    }


async def test_schedule_agent_dialogue_due_at_is_now():
    link = FakeNodeLink()
    before = datetime.now(UTC)
    await tools.schedule_agent_dialogue(link, 222, [{"role": "user", "content": "x"}], "off", 30.0)
    after = datetime.now(UTC)
    due_at = datetime.fromisoformat(link.calls[0][1]["due_at"])
    assert before <= due_at <= after


async def test_schedule_agent_dialogue_propagates_service_errors():
    link = FakeNodeLink(raises=ServiceUnavailableError("служба задач недоступна"))
    with pytest.raises(ServiceUnavailableError):
        await tools.schedule_agent_dialogue(
            link, 222, [{"role": "user", "content": "x"}], "off", 30.0
        )
