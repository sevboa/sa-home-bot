from __future__ import annotations

import asyncio
import json
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.context import FOLDED_OUTPUT_PREFIX
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import (
    REPEAT_HINT,
    REPEAT_SKIPPED,
    AgentLimits,
    AgentLoop,
    AgentStatus,
    Crane,
)
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


def make_loop(tmp_path, model, spec=None, limits=None, files=None, on_change=None, trace=None):
    for name, text in (files or DEFAULT_FILES).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    spec = spec or AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",))
    sandbox = LocalSandbox(tmp_path)
    loop = AgentLoop(
        model,
        ToolBox(sandbox, spec),
        FinishGuard(sandbox, spec),
        limits,
        on_change=on_change,
        trace=trace,
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
    runs = [tool_reply(TOOL_RUN_COMMAND, f"r{i}", command=f"{command} {i}") for i in range(4)]
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
        *[tool_reply(TOOL_RUN_TESTS, f"t{i}", paths=f"t{i}.py") for i in range(6)],
        tool_reply(TOOL_FINISH, "f", summary="ok"),
    )
    limits = AgentLimits(context_tokens=4000, reply_tokens=300, keep_recent=1)
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=limits)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    folded = model.seen[-1][2]["tool_calls"][0]["function"]["arguments"]
    parsed = json.loads(folded)
    assert parsed["path"] == "src/gen.py"
    assert "обрезано" in parsed["content"]


async def test_context_overflow_when_nothing_to_fold(tmp_path):
    model = ScriptedChatModel()
    limits = AgentLimits(context_tokens=3000, reply_tokens=500)
    loop, _ = make_loop(tmp_path, model, limits=limits)
    result = await loop.run("s", "я" * 20000)
    assert result.status is AgentStatus.CONTEXT_OVERFLOW
    assert result.steps == 0
    assert "системное сообщение 1 симв." in result.reason
    assert "задание 20000 симв." in result.reason
    assert "[agent] rules" in result.reason


async def test_long_tail_of_big_outputs_converges(tmp_path):
    spec = AgentSpec(
        test_argv=GREEN_TEST,
        diff_prefixes=("src/",),
        allowed_commands=(sys.executable,),
        output_chars=6000,
    )
    command = f"{sys.executable} -c \"print('x'*6000)\""
    runs = [tool_reply(TOOL_RUN_COMMAND, f"r{i}", command=f"{command} {i}") for i in range(12)]
    model = ScriptedChatModel(*runs, edit("e"), tool_reply(TOOL_FINISH, "f", summary="ok"))
    limits = AgentLimits(context_tokens=4000, reply_tokens=300, keep_recent=4)
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=limits)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    last = model.seen[-1]
    ids = {c["id"] for m in last if m["role"] == "assistant" for c in m.get("tool_calls") or ()}
    assert all(m["tool_call_id"] in ids for m in last if m["role"] == "tool")


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


def provider_failure(code, message="апстрим упал"):
    return {"error": {"message": message, "code": code}}


@pytest.mark.parametrize(
    ("code", "transient"),
    [(429, True), (502, True), (None, True), ("upstream", True), (400, False), (401, False)],
)
def test_parse_reply_provider_error_body(code, transient, caplog):
    with caplog.at_level("WARNING"), pytest.raises(ModelError, match="апстрим упал") as info:
        parse_reply(provider_failure(code))
    assert info.value.transient is transient
    assert "апстрим упал" in caplog.text


def test_parse_reply_string_error_is_transient():
    with pytest.raises(ModelError, match="сломалось") as info:
        parse_reply({"error": "сломалось"})
    assert info.value.transient


def test_parse_reply_empty_choices_is_transient_and_logs_raw(caplog):
    with caplog.at_level("WARNING"), pytest.raises(ModelError, match="нет choices") as info:
        parse_reply({"choices": [], "id": "gen-1"})
    assert info.value.transient
    assert "gen-1" in caplog.text


def test_parse_reply_finish_reason_error():
    body = {
        "choices": [
            {
                "finish_reason": "error",
                "error": {"message": "модель оборвалась", "code": 503},
                "message": {"content": ""},
            }
        ]
    }
    with pytest.raises(ModelError, match="модель оборвалась") as info:
        parse_reply(body)
    assert info.value.transient
    bare = {"choices": [{"finish_reason": "error", "message": {"content": ""}}]}
    with pytest.raises(ModelError, match="finish_reason=error") as info:
        parse_reply(bare)
    assert info.value.transient


def test_parse_reply_generates_missing_call_ids():
    calls = [
        {"type": "function", "function": {"name": "grep", "arguments": "{}"}},
        {"id": "", "function": {"name": "read", "arguments": None}},
        {"id": "own", "function": {"name": "ls"}},
    ]
    reply = parse_reply(reply_body("", calls), step=7)
    assert [c.id for c in reply.tool_calls] == ["call_7_0", "call_7_1", "own"]
    assert reply.tool_calls[1].arguments == "{}"


def test_parse_reply_call_without_name_is_error_and_log_is_truncated(caplog):
    calls = [{"id": "a", "function": {"arguments": "x" * 5000}}]
    with caplog.at_level("WARNING"), pytest.raises(ModelError, match="без имени") as info:
        parse_reply(reply_body("", calls))
    assert not info.value.transient
    assert 0 < len(caplog.text) < 2500


async def test_gateway_chat_200_with_error_body_is_model_error():
    async def handler(request):
        return web.json_response(provider_failure(502))

    server, chat = await run_gateway(handler)
    try:
        with pytest.raises(ModelError, match="апстрим упал") as info:
            await chat.complete([], [])
    finally:
        await chat.close()
        await server.close()
    assert info.value.transient


def test_model_timeout_is_at_least_half_hour():
    assert MODEL_TIMEOUT_S >= 1800


ECHO_ARGV = (sys.executable, "-c", "import sys; print('ARGS', sys.argv[1:])")


def toolbox(tmp_path, **spec):
    return ToolBox(LocalSandbox(tmp_path), AgentSpec(**spec))


async def test_write_file_appends_final_newline(tmp_path):
    box = toolbox(tmp_path)
    outcome = await box.call(TOOL_WRITE_FILE, {"path": "a.txt", "content": "x"})
    assert (tmp_path / "a.txt").read_bytes() == b"x\n"
    assert "перевод строки" in outcome.text
    outcome = await box.call(TOOL_WRITE_FILE, {"path": "b.txt", "content": "x\n"})
    assert (tmp_path / "b.txt").read_bytes() == b"x\n"
    assert "перевод строки" not in outcome.text


async def test_write_file_leaves_empty_and_binary_alone(tmp_path):
    box = toolbox(tmp_path)
    await box.call(TOOL_WRITE_FILE, {"path": "e.txt", "content": ""})
    await box.call(TOOL_WRITE_FILE, {"path": "n.bin", "content": "a\0b"})
    assert (tmp_path / "e.txt").read_bytes() == b""
    assert (tmp_path / "n.bin").read_bytes() == b"a\0b"


async def test_edit_file_appends_final_newline(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"one")
    outcome = await toolbox(tmp_path).call(
        TOOL_EDIT_FILE, {"path": "a.txt", "old": "one", "new": "two"}
    )
    assert (tmp_path / "a.txt").read_bytes() == b"two\n"
    assert "перевод строки" in outcome.text


async def test_run_tests_paths_narrow_the_run(tmp_path):
    box = toolbox(tmp_path, test_argv=ECHO_ARGV)
    assert "ARGS []" in (await box.call(TOOL_RUN_TESTS, {})).text
    narrow = await box.call(TOOL_RUN_TESTS, {"paths": "tests/unit/a.py  tests/b.py"})
    assert "ARGS ['tests/unit/a.py', 'tests/b.py']" in narrow.text
    assert narrow.green is True


async def test_run_tests_paths_outside_root_rejected(tmp_path):
    box = toolbox(tmp_path, test_argv=ECHO_ARGV)
    with pytest.raises(ToolError):
        await box.call(TOOL_RUN_TESTS, {"paths": "../x.py"})


async def test_write_file_keeps_crlf_style(tmp_path):
    box = toolbox(tmp_path)
    await box.call(TOOL_WRITE_FILE, {"path": "w.txt", "content": "a\r\nb"})
    await box.call(TOOL_WRITE_FILE, {"path": "m.txt", "content": "a\r\nb\nc"})
    assert (tmp_path / "w.txt").read_bytes() == b"a\r\nb\r\n"
    assert (tmp_path / "m.txt").read_bytes() == b"a\r\nb\nc\n"


async def test_run_tests_paths_reject_flags(tmp_path):
    box = toolbox(tmp_path, test_argv=ECHO_ARGV)
    with pytest.raises(ToolError, match="без флагов"):
        await box.call(TOOL_RUN_TESTS, {"paths": "tests/a.py --co"})


def test_guard_rejection_texts_ask_to_fix_and_retry():
    from sa_home_bot.devlab.agent.loop import FINISH_REJECTED, STEP_REJECTED

    assert "исправь и вызови" in FINISH_REJECTED
    assert "исправь и вызови" in STEP_REJECTED


async def test_trace_has_one_entry_per_step_and_cuts_long_arguments(tmp_path):
    entries = []
    long_content = "SECRET" * 200
    model = ScriptedChatModel(
        tool_reply(TOOL_WRITE_FILE, "w1", path="src/new.py", content=long_content),
        edit("c2", old="NO SUCH TEXT"),
        edit("c3"),
        tool_reply(TOOL_FINISH, "f1", summary="готово"),
    )
    loop, _ = make_loop(tmp_path, model, trace=entries.append)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED
    steps = [entry for entry in entries if "step" in entry]
    assert [entry["step"] for entry in steps] == [1, 2, 3, 4]
    assert entries[0]["event"] == "start"
    assert entries[-1]["event"] == "end"
    assert entries[-1]["status"] == "finished"
    first = steps[0]["calls"][0]
    assert first["name"] == TOOL_WRITE_FILE
    assert first["args"]["path"] == "src/new.py"
    assert len(first["args"]["content"]) < len(long_content)
    assert steps[0]["tokens_in"] == 100
    assert steps[1]["calls"][0]["result"].startswith("ошибка")
    assert steps[-1]["calls"][0]["name"] == TOOL_FINISH
    assert steps[-1]["calls"][0]["rejected"] is False


async def test_trace_marks_rejected_finish_and_crane(tmp_path):
    entries = []
    spec = AgentSpec(test_argv=RED_TEST, diff_prefixes=("src/",))
    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, "f1", summary="x"))
    loop, _ = make_loop(
        tmp_path, model, spec=spec, limits=AgentLimits(max_steps=2), trace=entries.append
    )
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.ASKED
    rejected = [c for e in entries for c in e.get("calls", []) if c["rejected"]]
    assert [c["name"] for c in rejected] == [TOOL_FINISH]
    assert entries[-1]["event"] == "end"
    assert entries[-1]["crane"] == "steps"


async def test_trace_counts_calls_skipped_after_ask_owner(tmp_path):
    entries = []
    reply = ModelReply(
        "",
        (
            ToolCall("a1", "ask_owner", json.dumps({"question": "как?"})),
            ToolCall("a2", TOOL_READ_FILE, json.dumps({"path": "src/app.py"})),
            ToolCall("a3", TOOL_READ_FILE, json.dumps({"path": "src/app.py"})),
        ),
    )
    loop, _ = make_loop(tmp_path, ScriptedChatModel(reply), trace=entries.append)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.ASKED
    step = next(entry for entry in entries if "step" in entry)
    assert step["skipped"] == 2
    assert len(step["calls"]) == 1


async def test_trace_skipped_is_zero_by_default(tmp_path):
    entries = []
    model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, summary="ok"))
    loop, _ = make_loop(tmp_path, model, trace=entries.append)
    await loop.run("правила", "задача")
    assert [e["skipped"] for e in entries if "step" in e] == [0, 0]


def lint_call(call_id, command="echo x.py"):
    return tool_reply(TOOL_RUN_COMMAND, call_id, command=command)


def repeat_spec():
    return AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",), allowed_commands=("echo",))


async def test_repeated_call_gets_hint_then_crane(tmp_path):
    entries = []
    model = ScriptedChatModel(lint_call("a"), lint_call("b"), lint_call("c"), lint_call("d"))
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec(), trace=entries.append)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.ASKED
    assert result.crane is Crane.REPEAT
    assert result.steps == 3
    assert result.reason == "модель повторяет run_command echo x.py"
    assert result.dialog.call_id is None
    tool_results = [m["content"] for m in result.dialog.messages if m["role"] == "tool"]
    assert REPEAT_HINT not in tool_results[0]
    assert tool_results[1].endswith(REPEAT_HINT)
    assert tool_results[2] == REPEAT_SKIPPED
    assert entries[-1]["event"] == "end"
    assert entries[-1]["crane"] == "repeat"


async def test_repeat_after_file_change_is_not_a_crane(tmp_path):
    model = ScriptedChatModel(
        lint_call("a"),
        lint_call("b"),
        edit(),
        lint_call("c"),
        tool_reply(TOOL_FINISH, summary="ok"),
    )
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED


async def test_different_arguments_are_not_a_repeat(tmp_path):
    model = ScriptedChatModel(
        lint_call("a", "echo 1"),
        lint_call("b", "echo 2"),
        lint_call("c", "echo 3"),
        edit(),
        tool_reply(TOOL_FINISH, summary="ok"),
    )
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED


async def test_repeat_counter_resets_after_owner_answer(tmp_path):
    model = ScriptedChatModel(
        lint_call("a"),
        lint_call("b"),
        lint_call("c"),
        lint_call("d"),
        lint_call("e"),
        lint_call("f"),
    )
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    first = await loop.run("правила", "задача")
    assert first.crane is Crane.REPEAT
    answered = Dialog(messages=first.dialog.messages, question="q", answer="продолжай")
    second = await loop.run("правила", "задача", resume=answered)
    assert second.crane is Crane.REPEAT
    assert second.steps == 3


async def test_third_repeat_skips_siblings_without_dangling_call_ids(tmp_path):
    ls = ToolCall("s1", TOOL_READ_FILE, json.dumps({"path": "src/app.py"}))
    rep = ToolCall("s2", TOOL_RUN_COMMAND, json.dumps({"command": "echo x.py"}))
    single = lint_call("a")
    batch = ModelReply(
        "", (ls, rep, ToolCall("s3", TOOL_READ_FILE, json.dumps({"path": "src/app.py"})))
    )
    model = ScriptedChatModel(single, lint_call("b"), batch)
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    result = await loop.run("правила", "задача")
    assert result.crane is Crane.REPEAT
    messages = result.dialog.messages
    asked = {
        c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or ()
    }
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert sorted(answered) == sorted(asked)
    assert {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}["s3"] == (
        REPEAT_SKIPPED
    )


class CountingSandbox(LocalSandbox):
    digests_calls = 0

    async def changed_digests(self):
        self.digests_calls += 1
        return await super().changed_digests()


async def test_tree_is_snapshotted_once_across_read_only_calls(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n")
    sandbox = CountingSandbox(tmp_path)
    spec = repeat_spec()
    reads = [
        tool_reply(TOOL_READ_FILE, f"r{i}", path="src/app.py", start_line=i + 1) for i in range(4)
    ]
    model = ScriptedChatModel(*reads, edit(), lint_call("l"), ModelError("конец"))
    loop = AgentLoop(model, ToolBox(sandbox, spec), FinishGuard(sandbox, spec))
    await loop.run("правила", "задача")
    assert sandbox.digests_calls == 2
