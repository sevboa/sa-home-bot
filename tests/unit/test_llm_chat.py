from __future__ import annotations

from types import SimpleNamespace

import pytest

from sa_home_bot import llm_chat
from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.service_link import ServiceTimeoutError, ServiceUnavailableError
from sa_home_bot.config import LlmConfig, Settings
from sa_home_bot.proto.messages import (
    ERR_INTERNAL,
    ERR_TIMEOUT,
    ERR_UNAVAILABLE,
    Address,
    ProtoError,
)

DST = Address(node="mycraft", service="llm")


class FakeNodeLink:
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
    assert events == []


async def test_on_tool_start_none_is_backward_compatible():
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


async def test_long_tool_result_is_shortened_but_full_text_reaches_on_tool_call(monkeypatch):
    long_result = "x" * (llm_chat._TOOL_RESULT_INLINE_LIMIT + 500)

    async def handler(ctx, args):
        return long_result

    toolkit = ai_tools.ToolKit(
        declarations=[{"type": "function", "function": {"name": "known_tool"}}],
        handlers={"known_tool": handler},
    )
    monkeypatch.setattr(ai_tools, "tools_for", lambda subscription: toolkit)

    seen_by_on_tool_call = []

    async def on_call(name, args, result):
        seen_by_on_tool_call.append(result)

    messages: list[dict] = []
    ctx = _ctx()
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
        messages,
        ctx,
        reason="off",
        telegram_chat_id=None,
        log_chat_id="test",
        on_tool_call=on_call,
    )

    assert result == "финальный ответ"
    assert seen_by_on_tool_call == [long_result]
    tool_message = next(m for m in messages if m.get("role") == "tool")
    assert len(tool_message["content"]) < len(long_result)
    assert "сокращено" in tool_message["content"]
    assert len(ctx.tool_result_cache) == 1
    (cached_id, cached_full) = next(iter(ctx.tool_result_cache.items()))
    assert cached_id in tool_message["content"]
    assert cached_full == long_result


async def test_short_tool_result_is_not_cached(monkeypatch):
    async def handler(ctx, args):
        return "короткий результат"

    toolkit = ai_tools.ToolKit(
        declarations=[{"type": "function", "function": {"name": "known_tool"}}],
        handlers={"known_tool": handler},
    )
    monkeypatch.setattr(ai_tools, "tools_for", lambda subscription: toolkit)

    messages: list[dict] = []
    ctx = _ctx()
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_tool_call("known_tool")]},
            {"response": "финальный ответ"},
        ]
    )

    await llm_chat.run_chat_loop(
        link, DST, 5.0, messages, ctx, reason="off", telegram_chat_id=None, log_chat_id="test"
    )

    tool_message = next(m for m in messages if m.get("role") == "tool")
    assert tool_message["content"] == "короткий результат"
    assert ctx.tool_result_cache == {}


@pytest.fixture
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr(llm_chat, "CHAT_RETRY_DELAYS_S", (0.0, 0.0))


async def _run(link):
    return await llm_chat.run_chat_loop(
        link, DST, 5.0, [], _ctx(), reason="off", telegram_chat_id=None, log_chat_id="test"
    )


async def test_раунд_после_тула_переживает_обрыв_связи(no_retry_sleep):
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_tool_call("known_tool")]},
            ProtoError(ERR_UNAVAILABLE, "mycraft: переподключение"),
            ServiceUnavailableError("нет соединения"),
            {"response": "финальный ответ"},
        ]
    )

    assert await _run(link) == "финальный ответ"


async def test_повторы_конечны(no_retry_sleep):
    link = FakeNodeLink(
        chat_results=[ProtoError(ERR_UNAVAILABLE, "нет связи")] * 3 + [{"response": "поздно"}]
    )

    with pytest.raises(ProtoError):
        await _run(link)


@pytest.mark.parametrize(
    "error",
    [
        ServiceTimeoutError("нет ответа"),
        ProtoError(ERR_TIMEOUT, "mycraft: нет ответа"),
        ProtoError(ERR_INTERNAL, "сбой"),
    ],
)
async def test_таймаут_и_сбои_не_повторяются(no_retry_sleep, error):
    link = FakeNodeLink(chat_results=[error, {"response": "не должно дойти"}])

    with pytest.raises(type(error)):
        await _run(link)


async def test_speech_user_and_clear_flag_ride_every_round():
    seen: list[dict] = []

    class RecordingLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            seen.append(dict(args))
            return await super().command(action, args, dst=dst, timeout=timeout)

    link = RecordingLink(
        chat_results=[{"tool_calls": [_tool_call("known_tool")]}, {"response": "ответ"}]
    )
    await llm_chat.run_chat_loop(
        link, DST, 10.0, [{"role": "user", "content": "hi"}], _ctx(), "off",
        telegram_chat_id=-100, log_chat_id=-100, speech_user_id=42, speech_clear=True,
    )
    assert [(a["user_id"], a["speech_clear"]) for a in seen] == [(42, True), (42, True)]


async def test_speech_clear_getter_is_reread_every_round():
    seen: list[dict] = []
    flags = iter([False, True])

    async def fresh() -> bool:
        return next(flags)

    class RecordingLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            seen.append(dict(args))
            return await super().command(action, args, dst=dst, timeout=timeout)

    link = RecordingLink(
        chat_results=[{"tool_calls": [_tool_call("known_tool")]}, {"response": "ответ"}]
    )
    await llm_chat.run_chat_loop(
        link, DST, 10.0, [{"role": "user", "content": "hi"}], _ctx(), "off",
        telegram_chat_id=-100, log_chat_id=-100, speech_user_id=42, speech_clear=fresh,
    )
    assert [a["speech_clear"] for a in seen] == [False, True]


async def test_no_speech_user_means_no_speech_args():
    seen: list[dict] = []

    class RecordingLink(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            seen.append(dict(args))
            return await super().command(action, args, dst=dst, timeout=timeout)

    await llm_chat.run_chat_loop(
        RecordingLink(chat_results=[{"response": "ответ"}]), DST, 10.0,
        [{"role": "user", "content": "hi"}], _ctx(), "off",
        telegram_chat_id=1, log_chat_id=1,
    )
    assert "user_id" not in seen[0] and "speech_clear" not in seen[0]


async def test_stats_collect_window_counters_across_rounds():
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": [_tool_call("known_tool")], "prompt_eval_count": 1200,
             "eval_count": 30, "done_reason": "stop", "num_ctx": 32768},
            {"response": "обрыв", "prompt_eval_count": 1500, "eval_count": 900,
             "done_reason": "length", "num_ctx": 32768},
        ]
    )
    stats = llm_chat.ChatStats()

    result = await llm_chat.run_chat_loop(
        link, DST, 5.0, [], _ctx(), reason="off", telegram_chat_id=None,
        log_chat_id="test", stats=stats,
    )

    assert result == "обрыв"
    assert stats.first_prompt_tokens == 1200
    assert stats.max_prompt_tokens == 1500
    assert stats.eval_tokens == 930
    assert stats.truncated and stats.num_ctx == 32768 and stats.rounds == 2


async def test_allow_tools_false_sends_no_declarations_and_runs_no_tools():
    sent_tools: list = []
    ran: list = []

    class Link(FakeNodeLink):
        async def command(self, action, args=None, dst=None, timeout=None):
            sent_tools.append(args["tools"])
            return await super().command(action, args, dst, timeout)

    async def on_call(name, args, result):
        ran.append(name)

    link = Link(
        chat_results=[{"tool_calls": [_tool_call("known_tool")]}, {"response": "ответ"}]
    )
    result = await llm_chat.run_chat_loop(
        link, DST, 5.0, [], _ctx(), reason="off", telegram_chat_id=None,
        log_chat_id="test", on_tool_call=on_call, allow_tools=False,
    )
    assert result == "ответ"
    assert sent_tools == [[], []]
    assert ran == ["known_tool"]


async def test_tool_can_end_the_turn():
    async def ending(ctx, args):
        ctx.end_turn = True
        return "снимок делается"

    ai_tools.tools_for(None).handlers["known_tool"] = ending
    link = FakeNodeLink(
        chat_results=[{"tool_calls": [_tool_call("known_tool")]}, {"response": "лишнее"}]
    )
    result = await llm_chat.run_chat_loop(
        link, DST, 5.0, [], _ctx(), reason="off", telegram_chat_id=None, log_chat_id="test"
    )
    assert result == ""
    assert link._chat_results == [{"response": "лишнее"}]


async def test_act_calls_run_one_per_round_and_see_each_result():
    ran: list[str] = []
    results: list[str] = []

    async def act(ctx, args):
        ran.append(args["action"])
        return f"итог {len(ran)}"

    ai_tools.tools_for(None).handlers["act"] = act
    ctx = _ctx()
    ctx.interactives = SimpleNamespace(canary_ok=lambda user_id: True)
    two = [_tool_call("act", {"action": "a"}), _tool_call("act", {"action": "b"})]
    link = FakeNodeLink(
        chat_results=[
            {"tool_calls": two},
            {"tool_calls": [_tool_call("act", {"action": "b"})]},
            {"tool_calls": [_tool_call("act", {"action": "c"})]},
            {"tool_calls": [_tool_call("act", {"action": "d"})]},
            {"response": "рассказ"},
        ]
    )

    async def on_call(name, args, result):
        results.append(result)

    answer = await llm_chat.run_chat_loop(
        link,
        DST,
        5.0,
        [],
        ctx,
        reason="off",
        telegram_chat_id=None,
        log_chat_id="test",
        on_tool_call=on_call,
    )
    assert answer == "рассказ"
    assert ran == ["a", "b", "c", "d"]
    assert results[1] == ai_tools.SERIAL_DEFERRED
