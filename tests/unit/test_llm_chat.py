"""llm_chat.py::run_chat_loop — контракт хука on_tool_start (этап 34, живая
находка 2026-08-10): зовётся ДО хендлера тула, в отличие от on_tool_call
(после, с результатом — durable-запись, bot/ai_flow.py). Не зовётся для
неизвестного модели тула. Обратно совместим при on_tool_start=None (служба
tasks, tasks/service.py, этот параметр вообще не передаёт)."""

from __future__ import annotations

import pytest

from sa_home_bot import llm_chat
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.proto.messages import Address

DST = Address(node="mycraft", service="llm")


class FakeNodeLink:
    """Отвечает на command("chat", ...) по очереди — тот же приём, что
    FakeNodeLink в test_ai_flow.py, но без presence/wake-обвязки: этот файл
    проверяет только hook-контракт run_chat_loop, не сценарий /ai."""

    def __init__(self, chat_results):
        self._chat_results = list(chat_results)

    async def command(self, action, args=None, dst=None, timeout=None):
        assert action == "chat"
        result = self._chat_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def _fake_handler(ctx, args):
    return "ok"


@pytest.fixture(autouse=True)
def fake_toolkit(monkeypatch):
    """Подменяет tools_for на фиксированный комплект с одним известным
    тулом — реальные права/декларации (bot/tools.py::tools_for) здесь ни
    при чём, тестируется только порядок вызова хуков вокруг хендлера."""
    toolkit = ai_tools.ToolKit(
        declarations=[{"type": "function", "function": {"name": "known_tool"}}],
        handlers={"known_tool": _fake_handler},
    )
    monkeypatch.setattr(ai_tools, "tools_for", lambda subscription: toolkit)


def _tool_call(name: str, args: dict | None = None) -> dict:
    return {"function": {"name": name, "arguments": args or {}}}


def _ctx() -> ai_tools.ToolContext:
    return ai_tools.ToolContext(
        chat_id=1, dialogue_id=1, trigger_message_id=1, settings=Settings(llm=LlmConfig())
    )


async def test_on_tool_start_fires_before_handler_and_on_tool_call_after():
    events: list[tuple] = []

    async def on_start(name, args):
        events.append(("start", name))

    async def on_call(name, args, result):
        events.append(("call", name, result))

    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_tool_call("known_tool")]},
            {"response": "финальный ответ"},
        ]
    )

    result = await llm_chat.run_chat_loop(
        link,
        DST,
        5.0,
        [],
        _ctx(),
        reason="off",
        telegram_chat_id=None,
        log_chat_id="test",
        on_tool_call=on_call,
        on_tool_start=on_start,
    )

    assert result == "финальный ответ"
    assert events == [("start", "known_tool"), ("call", "known_tool", "ok")]


async def test_on_tool_start_not_called_for_unknown_tool():
    events: list[str] = []

    async def on_start(name, args):
        events.append(name)

    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_tool_call("ghost_tool")]},
            {"response": "финальный ответ"},
        ]
    )

    result = await llm_chat.run_chat_loop(
        link,
        DST,
        5.0,
        [],
        _ctx(),
        reason="off",
        telegram_chat_id=None,
        log_chat_id="test",
        on_tool_start=on_start,
    )

    assert result == "финальный ответ"
    assert events == []  # неизвестный тул — исполнять нечего, уведомлять не о чем


async def test_on_tool_start_none_is_backward_compatible():
    # Служба tasks (tasks/service.py) не передаёт on_tool_start вовсе —
    # дефолт None не должен ронять цикл.
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_tool_call("known_tool")]},
            {"response": "финальный ответ"},
        ]
    )

    result = await llm_chat.run_chat_loop(
        link, DST, 5.0, [], _ctx(), reason="off", telegram_chat_id=None, log_chat_id="test"
    )

    assert result == "финальный ответ"
