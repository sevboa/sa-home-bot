from __future__ import annotations

import asyncio
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.context import FOLDED_OUTPUT_PREFIX
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import AgentLimits, AgentLoop, AgentStatus
from sa_home_bot.devlab.agent.model import (
    MODEL_TIMEOUT_S,
    GatewayChat,
    ModelError,
    ModelReply,
    ToolCall,
    parse_reply,
)
from sa_home_bot.devlab.agent.sandbox import LocalSandbox, SandboxPathError
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    TOOL_EDIT_FILE,
    TOOL_FINISH,
    TOOL_GREP,
    TOOL_READ_FILE,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
    TOOL_WRITE_FILE,
    ToolBox,
    ToolError,
)
from tests.unit.support.fakes import ScriptedChatModel, tool_reply

GREEN_TEST = (sys.executable, "-c", "import sys; sys.exit(0)")
RED_TEST = (sys.executable, "-c", "print('boom'); import sys; sys.exit(1)")
DEFAULT_FILES = {"src/app.py": "VALUE = 1\n"}


def make_loop(tmp_path, model, spec=None, limits=None, files=None, on_change=None):
    for name, text in (files or DEFAULT_FILES).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    spec = spec or AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",))
    sandbox = LocalSandbox(tmp_path)
    loop = AgentLoop(
        model, ToolBox(sandbox, spec), FinishGuard(sandbox, spec), limits, on_change=on_change
    )
    return loop, sandbox


def edit(call_id="c1", old="VALUE = 1", new="VALUE = 2"):
    return tool_reply(TOOL_EDIT_FILE, call_id, path="src/app.py", old=old, new=new)


async def test_edit_then_finish(tmp_path):
    changes = []

    async def on_change(text):
        changes.append(text)

    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, summary=" готово "))
    loop, _ = make_loop(tmp_path, model, on_change=on_change)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED
    assert result.summary == "готово"
    assert result.steps == 2
    assert (result.tokens_in, result.tokens_out) == (200, 20)
    assert (tmp_path / "src/app.py").read_text() == "VALUE = 2\n"
    assert changes == ["edit_file: правка src/app.py: заменён один фрагмент"]
    assert model.seen[0][0]["content"] == "правила"
    assert model.seen[1][-1]["role"] == "tool"


async def test_finish_without_diff_rejected(tmp_path):
    model = ScriptedChatModel(
        tool_reply(TOOL_FINISH, summary="готово"),
        edit("c2"),
        tool_reply(TOOL_FINISH, "c3", summary="теперь правда"),
    )
    loop, _ = make_loop(tmp_path, model)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    rejected = model.seen[1][-1]["content"]
    assert rejected.startswith("финал отклонён") and "нет изменений в src/" in rejected


async def test_finish_ignores_diff_outside_prefix(tmp_path):
    model = ScriptedChatModel(
        tool_reply(TOOL_WRITE_FILE, path="notes.txt", content="x"),
        tool_reply(TOOL_FINISH, summary="готово"),
        tool_reply(TOOL_RUN_TESTS, "t"),
    )
    loop, _ = make_loop(tmp_path, model, limits=AgentLimits(max_steps=3))
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.ASKED
    assert "нет изменений" in model.seen[-1][-1]["content"]


async def test_finish_rejected_on_red_tests_and_checks(tmp_path):
    spec = AgentSpec(
        test_argv=RED_TEST,
        checks=((sys.executable, "-c", "print('страж'); raise SystemExit(2)"),),
        diff_prefixes=("src/",),
    )
    model = ScriptedChatModel(
        edit(), tool_reply(TOOL_FINISH, summary="готово"), tool_reply(TOOL_RUN_TESTS, "t")
    )
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=AgentLimits(max_steps=3))
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.ASKED
    answer = model.seen[-1][-1]["content"]
    assert "тесты не зелёные" in answer and "boom" in answer and "страж" in answer


async def test_tool_errors_do_not_stop_loop(tmp_path):
    broken_json = ModelReply("", (ToolCall("bad", TOOL_EDIT_FILE, "{не json"),))
    model = ScriptedChatModel(
        edit(old="нет такого"),
        tool_reply(TOOL_READ_FILE, path="missing.py"),
        tool_reply("nonexistent"),
        tool_reply(TOOL_WRITE_FILE, path="../escape.txt", content="x"),
        tool_reply(TOOL_RUN_COMMAND, command="rm -rf /"),
        broken_json,
        edit(),
        tool_reply(TOOL_FINISH, summary="готово"),
    )
    loop, _ = make_loop(tmp_path, model)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    answers = [m["content"] for m in model.seen[-1] if m["role"] == "tool"]
    assert len(answers) == 7
    assert all(a.startswith("ошибка") for a in answers[:6])
    assert "не являются JSON" in answers[5]
    assert not (tmp_path.parent / "escape.txt").exists()


async def test_unexpected_tool_exception_is_reported(tmp_path, monkeypatch):
    model = ScriptedChatModel(tool_reply(TOOL_RUN_TESTS), tool_reply(TOOL_FINISH, summary="x"))
    loop, sandbox = make_loop(tmp_path, model, limits=AgentLimits(max_steps=2))

    async def broken(argv, timeout_s):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(sandbox, "exec", broken)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.ASKED
    assert model.seen[1][-1]["content"] == "ошибка инструмента: RuntimeError: сломалось"


async def test_plain_text_replies_nudged_then_stuck(tmp_path):
    model = ScriptedChatModel(*[ModelReply("готово!")] * 4)
    loop, _ = make_loop(tmp_path, model, limits=AgentLimits(max_idle_replies=3))
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.STUCK
    assert result.steps == 4
    assert model.seen[1][-1]["role"] == "user"


async def test_model_error_ends_with_reason(tmp_path):
    model = ScriptedChatModel(edit(), ModelError("модель не ответила за 1800 с"))
    loop, _ = make_loop(tmp_path, model)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.MODEL_FAILED
    assert result.reason == "модель не ответила за 1800 с"
    assert result.tokens_in == 100


async def test_old_tool_outputs_are_folded(tmp_path):
    spec = AgentSpec(
        test_argv=GREEN_TEST,
        diff_prefixes=("src/",),
        allowed_commands=(sys.executable,),
        output_chars=5000,
    )
    command = f"{sys.executable} -c \"print('x'*5000)\""
    runs = [tool_reply(TOOL_RUN_COMMAND, f"r{i}", command=command) for i in range(4)]
    model = ScriptedChatModel(*runs, edit("e"), tool_reply(TOOL_FINISH, "f", summary="ok"))
    limits = AgentLimits(context_tokens=7000, reply_tokens=500, keep_recent=2)
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=limits)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    tool_texts = [m["content"] for m in model.seen[-1] if m["role"] == "tool"]
    assert tool_texts[0].startswith(FOLDED_OUTPUT_PREFIX)
    assert not tool_texts[-2].startswith(FOLDED_OUTPUT_PREFIX)
    assert len(model.seen[-1]) > 8


async def test_large_write_arguments_are_folded(tmp_path):
    big = tool_reply(TOOL_WRITE_FILE, "w", path="src/gen.py", content="x = 1\n" * 800)
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",))
    model = ScriptedChatModel(
        big,
        *[tool_reply(TOOL_RUN_TESTS, f"t{i}") for i in range(6)],
        tool_reply(TOOL_FINISH, "f", summary="ok"),
    )
    limits = AgentLimits(context_tokens=4000, reply_tokens=300, keep_recent=1)
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=limits)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    folded = model.seen[-1][2]["tool_calls"][0]["function"]["arguments"]
    assert "свёрнуто" in folded


async def test_context_overflow_when_nothing_to_fold(tmp_path):
    model = ScriptedChatModel()
    limits = AgentLimits(context_tokens=3000, reply_tokens=500)
    loop, _ = make_loop(tmp_path, model, limits=limits)
    result = await loop.run("s", "я" * 20000)
    assert result.status is AgentStatus.CONTEXT_OVERFLOW
    assert result.steps == 0


async def test_forbidden_paths_and_command_whitelist(tmp_path):
    spec = AgentSpec(forbidden_paths=("tests/*", "docs/"), allowed_commands=("echo",))
    box = ToolBox(LocalSandbox(tmp_path), spec)
    for path in ("tests/test_a.py", "docs/x.md"):
        with pytest.raises(ToolError, match="закрыт"):
            await box.call(TOOL_WRITE_FILE, {"path": path, "content": "x"})
    outcome = await box.call(TOOL_RUN_COMMAND, {"command": "echo привет"})
    assert outcome.mutating and "привет" in outcome.text
    with pytest.raises(ToolError, match="не разрешена"):
        await box.call(TOOL_RUN_COMMAND, {"command": "ls"})


async def test_command_timeout_reported(tmp_path):
    spec = AgentSpec(allowed_commands=("sleep",), command_timeout_s=0.2)
    box = ToolBox(LocalSandbox(tmp_path), spec)
    outcome = await box.call(TOOL_RUN_COMMAND, {"command": "sleep 5"})
    assert outcome.text.startswith("[таймаут 0.2 с]")


async def test_sandbox_tracks_changes_and_rejects_escape(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    sandbox = LocalSandbox(tmp_path)
    assert await sandbox.changed_paths() == []
    await sandbox.write_bytes("dir/b.txt", b"2")
    (tmp_path / "a.txt").unlink()
    assert await sandbox.changed_paths() == ["a.txt", "dir/b.txt"]
    with pytest.raises(SandboxPathError):
        await sandbox.write_bytes("/etc/x", b"")


async def test_read_and_grep(tmp_path):
    (tmp_path / "m.py").write_text("\n".join(f"line{i}" for i in range(1, 501)))
    box = ToolBox(LocalSandbox(tmp_path), AgentSpec())
    out = (await box.call(TOOL_READ_FILE, {"path": "m.py", "start_line": 10})).text
    assert out.splitlines()[0] == "m.py, строки 10-209 из 500"
    assert "10\tline10" in out
    found = (await box.call(TOOL_GREP, {"pattern": r"line49\d"})).text
    assert found.splitlines()[0] == "m.py:490:line490"


def reply_body(content="", calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}], "usage": usage or {}}


async def run_gateway(handler, timeout_s=5.0):
    app = web.Application()
    app.router.add_post(protocol.HTTP_CHAT_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    chat = GatewayChat(str(server.make_url("")), "tok", "flint", "job-1", timeout_s=timeout_s)
    return server, chat


async def test_gateway_chat_request_and_parse():
    seen = {}

    async def handler(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = await request.json()
        call = {"id": "k", "type": "function", "function": {"name": "grep", "arguments": "{}"}}
        usage = {"prompt_tokens": 7, "completion_tokens": 3}
        return web.json_response(reply_body("мысль", [call], usage))

    server, chat = await run_gateway(handler)
    try:
        reply = await chat.complete([{"role": "user", "content": "x"}], [{"type": "function"}])
    finally:
        await chat.close()
        await server.close()
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert seen["headers"][protocol.HEADER_JOB] == "job-1"
    assert seen["body"]["model"] == "flint" and seen["body"]["tools"] == [{"type": "function"}]
    assert reply.content == "мысль"
    assert reply.tool_calls[0].name == "grep"
    assert (reply.tokens_in, reply.tokens_out) == (7, 3)
    assert reply.as_message()["tool_calls"][0]["function"]["name"] == "grep"


async def test_gateway_chat_timeout_and_http_error():
    async def slow(request):
        await asyncio.sleep(5)
        return web.json_response({})

    async def refused(request):
        return web.json_response({"error": "суточный лимит исчерпан"}, status=429)

    server, chat = await run_gateway(slow, timeout_s=0.2)
    try:
        with pytest.raises(ModelError, match="не ответила за 0.2 с"):
            await chat.complete([], [])
    finally:
        await chat.close()
        await server.close()
    server, chat = await run_gateway(refused)
    try:
        with pytest.raises(ModelError, match="HTTP 429.*суточный лимит"):
            await chat.complete([], [])
    finally:
        await chat.close()
        await server.close()


def test_parse_reply_rejects_garbage():
    with pytest.raises(ModelError):
        parse_reply({"choices": []})
    assert parse_reply(reply_body("текст")).tool_calls == ()


def test_model_timeout_is_at_least_half_hour():
    assert MODEL_TIMEOUT_S >= 1800
