from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent import tools as tools_module
from sa_home_bot.devlab.agent.context import FOLDED_OUTPUT, FOLDED_OUTPUT_PREFIX, REASONING_FIELD
from sa_home_bot.devlab.agent.dialog import Dialog
from sa_home_bot.devlab.agent.guard import FinishGuard
from sa_home_bot.devlab.agent.loop import (
    CODE_FINISH_REJECTED,
    CODE_NUDGE_TEXT,
    CODE_REPEAT_TURN,
    FINISH_REJECTED,
    NUDGE_TEXT,
    REPEAT_SKIPPED,
    REPEAT_TURN,
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
from sa_home_bot.devlab.agent.prompt import (
    CODE_MODE_REMINDER_CB_V1,
    CODE_MODE_REMINDER_CB_V2,
    CODE_MODE_REMINDER_CB_V3,
    CODE_MODE_REMINDER_REMOTE,
    FLOW_REMINDER,
    with_flow_reminder,
)
from sa_home_bot.devlab.agent.prompt_en import CODE_MODE_REMINDER_REMOTE_EN
from sa_home_bot.devlab.agent.sandbox import (
    ExecResult,
    LocalSandbox,
    SandboxPathError,
    failed_test_ids,
)
from sa_home_bot.devlab.agent.script import worker_argv
from sa_home_bot.devlab.agent.spec import AgentSpec
from sa_home_bot.devlab.agent.tools import (
    TOOL_EDIT_FILE,
    TOOL_EXECUTE,
    TOOL_FINISH,
    TOOL_GREP,
    TOOL_READ_FILE,
    TOOL_RUN_COMMAND,
    TOOL_RUN_TESTS,
    TOOL_WRITE_FILE,
    ToolBox,
    ToolError,
    read_max_lines,
)
from tests.unit.support.fakes import ScriptedChatModel, tool_reply

GREEN_TEST = (sys.executable, "-c", "import sys; sys.exit(0)")
RED_TEST = (sys.executable, "-c", "print('boom'); import sys; sys.exit(1)")
DEFAULT_FILES = {"src/app.py": "VALUE = 1\n"}


def make_loop(
    tmp_path,
    model,
    spec=None,
    limits=None,
    files=None,
    on_change=None,
    trace=None,
    clock=None,
    on_fold=None,
):
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
        on_fold=on_fold,
        **({"clock": clock} if clock else {}),
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


async def test_finish_allow_no_changes_accepted(tmp_path):
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",), allow_no_changes=True)
    model = ScriptedChatModel(tool_reply(TOOL_FINISH, summary="готово"))
    loop, _ = make_loop(tmp_path, model, spec=spec)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    assert result.summary == "готово"


@pytest.mark.parametrize(
    "summary,accepted",
    [
        ("ВЕРДИКТ: принято", True),
        ("итог\nВЕРДИКТ: доработать\n- пункт", True),
        ("всё хорошо", False),
    ],
)
async def test_finish_require_verdict(tmp_path, summary, accepted):
    spec = AgentSpec(
        test_argv=GREEN_TEST,
        diff_prefixes=("src/",),
        require_verdict=True,
    )
    if accepted:
        model = ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, summary=summary))
    else:
        model = ScriptedChatModel(
            edit(),
            tool_reply(TOOL_FINISH, summary=summary),
            tool_reply(TOOL_FINISH, summary="ВЕРДИКТ: принято"),
        )
    loop, _ = make_loop(tmp_path, model, spec=spec)
    result = await loop.run("s", "t")
    if accepted:
        assert result.status is AgentStatus.FINISHED
        assert result.summary == summary
    else:
        assert result.status is AgentStatus.FINISHED
        assert result.summary == "ВЕРДИКТ: принято"
        rejected = model.seen[2][-1]["content"]
        assert "нет вердикта" in rejected


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
    model = ScriptedChatModel(
        tool_reply(TOOL_RUN_TESTS, paths="tests/unit/a.py"), tool_reply(TOOL_FINISH, summary="x")
    )
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


@pytest.mark.parametrize(
    ("code_mode", "nudge", "rejected"),
    [(False, NUDGE_TEXT, FINISH_REJECTED), (True, CODE_NUDGE_TEXT, CODE_FINISH_REJECTED)],
)
async def test_nudge_and_finish_rejection_follow_code_mode(tmp_path, code_mode, nudge, rejected):
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",), code_mode=code_mode)
    model = ScriptedChatModel(ModelReply("готово!"), tool_reply(TOOL_FINISH, summary="x"))
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=AgentLimits(max_steps=2))
    result = await loop.run("s", "t")
    assert model.seen[1][-1] == {"role": "user", "content": nudge}
    assert result.dialog.messages[-1]["content"].startswith(rejected)
    assert ("в execute" in nudge) is code_mode


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


async def test_fold_callback_fires_only_when_history_shrinks(tmp_path):
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
    folds = []
    loop, _ = make_loop(
        tmp_path, model, spec=spec, limits=limits, on_fold=lambda: folds.append(1)
    )
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    assert folds

    quiet = []
    plain_loop, _ = make_loop(
        tmp_path / "q",
        ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, summary="ok")),
        on_fold=lambda: quiet.append(1),
    )
    await plain_loop.run("s", "t")
    assert quiet == []


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


async def test_locked_path_rejects_write_but_allows_read(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text("ok\n")
    box = ToolBox(LocalSandbox(tmp_path), AgentSpec(locked_paths=("tests/test_a.py",)))
    with pytest.raises(ToolError, match="файл тестера"):
        await box.call(TOOL_WRITE_FILE, {"path": "tests/test_a.py", "content": "x"})
    outcome = await box.call(TOOL_READ_FILE, {"path": "tests/test_a.py"})
    assert "ok" in outcome.text
    await box.call(TOOL_WRITE_FILE, {"path": "tests/test_b.py", "content": "x"})


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


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (TOOL_WRITE_FILE, {"content": "x"}),
        (TOOL_EDIT_FILE, {"old": "old", "new": "new"}),
    ],
)
@pytest.mark.parametrize(
    ("write_prefixes", "path", "should_pass"),
    [
        (("tests/",), "tests/x.py", True),
        (("tests/",), "src/x.py", False),
        (("docs/",), "docs/readme.md", True),
        ((), "src/x.py", True),
    ],
)
async def test_write_prefixes(tmp_path, tool, args, write_prefixes, path, should_pass):
    (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / path).write_text("old")
    box = ToolBox(LocalSandbox(tmp_path), AgentSpec(write_prefixes=write_prefixes))
    if should_pass:
        await box.call(tool, {"path": path, **args})
    else:
        with pytest.raises(ToolError, match="вне путей роли"):
            await box.call(tool, {"path": path, **args})


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


@pytest.mark.parametrize(
    ("window", "expected"),
    [(0, 200), (30000, 500), (10**6, 2000)],
)
def test_read_max_lines(window, expected):
    assert read_max_lines(window) == expected


async def test_read_file_by_window(tmp_path):
    (tmp_path / "m.py").write_text("\n".join(f"line{i}" for i in range(1, 451)))
    box = ToolBox(LocalSandbox(tmp_path), AgentSpec(), read_lines=500)
    out = (await box.call(TOOL_READ_FILE, {"path": "m.py"})).text
    assert out.splitlines()[0] == "m.py, строки 1-450 из 450"


def reply_body(content="", calls=None, usage=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}], "usage": usage or {}}


async def run_gateway(handler, timeout_s=5.0, model="flint", **chat_kwargs):
    app = web.Application()
    app.router.add_post(protocol.HTTP_CHAT_PATH, handler)
    server = TestServer(app)
    await server.start_server()
    chat = GatewayChat(
        str(server.make_url("")), "tok", model, "job-1", timeout_s=timeout_s, **chat_kwargs
    )
    return server, chat


async def test_gateway_chat_uses_code_mode_reminder_when_execute_offered():
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return web.json_response(reply_body("ок", [], {}))

    server, chat = await run_gateway(handler)
    tools = ToolBox(LocalSandbox(Path(".")), AgentSpec(code_mode=True)).definitions
    try:
        await chat.complete([{"role": "user", "content": "x"}], tools)
    finally:
        await chat.close()
        await server.close()
    expected = f"x\n\n{CODE_MODE_REMINDER_REMOTE}"
    assert seen["body"]["messages"] == [{"role": "user", "content": expected}]


@pytest.mark.parametrize(
    ("model", "style", "expected"),
    [
        ("glm-5.3", "cb_v1", CODE_MODE_REMINDER_CB_V1),
        ("glm-5.3", "cb_v2", CODE_MODE_REMINDER_CB_V2),
        ("glm-5.3", "cb_v3", CODE_MODE_REMINDER_CB_V3),
        ("flint", "cb_v1", CODE_MODE_REMINDER_REMOTE),
    ],
)
async def test_gateway_chat_reminder_follows_prompt_style(model, style, expected):
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return web.json_response(reply_body("ок", [], {}))

    server, chat = await run_gateway(handler, model=model, prompt_style=style)
    tools = ToolBox(LocalSandbox(Path(".")), AgentSpec(code_mode=True)).definitions
    try:
        await chat.complete([{"role": "user", "content": "x"}], tools)
    finally:
        await chat.close()
        await server.close()
    assert seen["body"]["messages"] == [{"role": "user", "content": f"x\n\n{expected}"}]


async def test_gateway_chat_reminder_override_replaces_code_mode_reminder():
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return web.json_response(reply_body("ок", [], {}))

    server, chat = await run_gateway(handler, reminder="файловое напоминание")
    tools = ToolBox(LocalSandbox(Path(".")), AgentSpec(code_mode=True)).definitions
    try:
        await chat.complete([{"role": "user", "content": "x"}], tools)
    finally:
        await chat.close()
        await server.close()
    assert seen["body"]["messages"] == [{"role": "user", "content": "x\n\nфайловое напоминание"}]


async def test_gateway_chat_uses_english_reminder_for_english_definitions():
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return web.json_response(reply_body("ок", [], {}))

    server, chat = await run_gateway(handler)
    spec = AgentSpec(code_mode=True, prompt_lang="en")
    tools = ToolBox(LocalSandbox(Path(".")), spec).definitions
    try:
        await chat.complete([{"role": "user", "content": "x"}], tools)
    finally:
        await chat.close()
        await server.close()
    expected = f"x\n\n{CODE_MODE_REMINDER_REMOTE_EN}"
    assert seen["body"]["messages"] == [{"role": "user", "content": expected}]


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
    assert seen["body"]["messages"] == [{"role": "user", "content": f"x\n\n{FLOW_REMINDER}"}]
    assert seen["body"]["user"] == "devlab job-1"
    assert "temperature" not in seen["body"]
    assert reply.content == "мысль"
    assert reply.tool_calls[0].name == "grep"
    assert (reply.tokens_in, reply.tokens_out) == (7, 3)
    assert reply.as_message()["tool_calls"][0]["function"]["name"] == "grep"


async def test_gateway_chat_extra_goes_into_body():
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return web.json_response(reply_body("ok"))

    server, chat = await run_gateway(handler)
    extra = {"chat_template_kwargs": {"enable_thinking": False}}
    try:
        await chat.complete([{"role": "user", "content": "x"}], [], extra=extra)
    finally:
        await chat.close()
        await server.close()
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


class ExtraRecordingModel(ScriptedChatModel):
    def __init__(self, *script):
        super().__init__(*script)
        self.extras = []

    async def complete(self, messages, tools, **kwargs):
        self.extras.append(kwargs.get("extra"))
        return await super().complete(messages, tools)


@pytest.mark.parametrize(
    ("quick_steps", "quick_params", "expected"),
    [
        (2, {"think": False}, [{"think": False}, {"think": False}, None]),
        (2, {}, [None, None, None]),
        (0, {"think": False}, [None, None, None]),
    ],
)
async def test_quick_params_only_on_first_steps(tmp_path, quick_steps, quick_params, expected):
    entries = []
    model = ExtraRecordingModel(
        *(tool_reply(TOOL_GREP, f"g{n}", pattern=f"p{n}") for n in range(3))
    )
    limits = AgentLimits(max_steps=3, quick_steps=quick_steps, quick_params=quick_params)
    loop, _ = make_loop(tmp_path, model, limits=limits, trace=entries.append)
    await loop.run("правила", "задача")
    assert model.extras == expected
    quick = [entry.get("quick", False) for entry in entries if "step" in entry]
    assert quick == [extra is not None for extra in expected]


@pytest.mark.parametrize(
    ("params", "quick_params", "expected"),
    [
        ({"reasoning": {"enabled": True}}, {}, [{"reasoning": {"enabled": True}}] * 3),
        (
            {"reasoning": {"enabled": True}, "temperature": 1.0},
            {"reasoning": {"enabled": False}},
            [
                {"reasoning": {"enabled": False}, "temperature": 1.0},
                {"reasoning": {"enabled": True}, "temperature": 1.0},
                {"reasoning": {"enabled": True}, "temperature": 1.0},
            ],
        ),
    ],
)
async def test_profile_params_ride_on_every_step(tmp_path, params, quick_params, expected):
    model = ExtraRecordingModel(
        *(tool_reply(TOOL_GREP, f"g{n}", pattern=f"p{n}") for n in range(3))
    )
    limits = AgentLimits(max_steps=3, quick_steps=1, quick_params=quick_params, params=params)
    loop, _ = make_loop(tmp_path, model, limits=limits)
    await loop.run("правила", "задача")
    assert model.extras == expected


@pytest.mark.parametrize(
    "extra",
    [
        {REASONING_FIELD: "план"},
        {"reasoning": "план", "reasoning_details": [{"type": "reasoning.text", "text": "план"}]},
    ],
)
async def test_gateway_chat_reasoning_rides_in_history(extra):
    bodies = []

    async def handler(request):
        bodies.append(await request.json())
        call = {"id": f"k{len(bodies)}", "type": "function", "function": {"name": "grep"}}
        body = reply_body("", [call])
        body["choices"][0]["message"].update(extra)
        return web.json_response(body)

    server, chat = await run_gateway(handler)
    try:
        history = [{"role": "user", "content": "x"}]
        first = await chat.complete(history, [])
        history += [first.as_message(), {"role": "tool", "tool_call_id": "k1", "content": "ok"}]
        await chat.complete(history, [])
    finally:
        await chat.close()
        await server.close()
    assert first.reasoning == extra
    assert REASONING_FIELD not in ModelReply("ответ").as_message()
    sent = bodies[1]["messages"][1]
    assert {key: sent[key] for key in extra} == extra


def flow_step(key: str, output: str) -> list[dict]:
    call = {"id": key, "type": "function", "function": {"name": "t", "arguments": "{}"}}
    return [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": key, "content": output},
    ]


def test_flow_reminder_keeps_sent_prefix_unchanged():
    history = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    history += flow_step("k1", "a")
    sent = with_flow_reminder(history)
    longer = with_flow_reminder([*history, *flow_step("k2", "b"), *flow_step("k3", "c")])
    assert longer[: len(sent)] == sent
    assert sent[0]["content"] == "s"
    assert sent[1]["content"] == f"t\n\n{FLOW_REMINDER}"
    assert longer[-1]["content"] == f"c\n\n{FLOW_REMINDER}"
    assert history[-1]["content"] == "a"


def test_flow_reminder_only_on_step_end_and_not_on_folded_output():
    folded = FOLDED_OUTPUT.format(size=10)
    call = {"id": "k1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
    history = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "t"},
        {"role": "assistant", "content": "", "tool_calls": [call, {**call, "id": "k2"}]},
        {"role": "tool", "tool_call_id": "k1", "content": "первый"},
        {"role": "tool", "tool_call_id": "k2", "content": folded},
        *flow_step("k3", "b"),
    ]
    view = with_flow_reminder(history)
    assert [m["content"].endswith(FLOW_REMINDER) for m in view] == [
        False,
        True,
        False,
        False,
        False,
        False,
        True,
    ]
    assert view[4]["content"] == folded


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


@pytest.mark.parametrize(
    ("status", "body", "silence", "transient"),
    [
        (503, '{"error": "тишина видеокарты до /silent off", "code": "gpu_silence"}', True, True),
        (503, '{"error": "лежит"}', False, True),
    ],
)
async def test_gateway_chat_silence_detection(status, body, silence, transient):
    async def handler(request):
        return web.json_response(json.loads(body), status=status)

    server, chat = await run_gateway(handler)
    try:
        with pytest.raises(ModelError) as info:
            await chat.complete([], [])
    finally:
        await chat.close()
        await server.close()
    assert info.value.silence is silence
    assert info.value.transient is transient


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
    narrow = await box.call(TOOL_RUN_TESTS, {"paths": "tests/unit/a.py  tests/b.py"})
    assert "ARGS ['tests/unit/a.py', 'tests/b.py']" in narrow.text
    assert narrow.green is True


@pytest.mark.parametrize(
    "xdist",
    [
        ("-n", "auto"),
        ("-n=auto",),
        ("-nauto",),
        ("-n4",),
        ("--numprocesses", "4"),
        ("--numprocesses=4",),
    ],
)
async def test_run_tests_narrow_strips_xdist(tmp_path, xdist):
    box = toolbox(
        tmp_path,
        test_argv=("echo", "pytest", "-q", *xdist, "-x"),
        test_timeout_s=3600.0,
        narrow_test_timeout_s=120.0,
    )
    narrow = await box.call(TOOL_RUN_TESTS, {"paths": "tests/unit/a.py"})
    assert "pytest -q -x tests/unit/a.py" in narrow.text


class ExecSpySandbox(LocalSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.exec_calls = 0

    async def exec(self, argv, timeout_s):
        self.exec_calls += 1
        return await super().exec(argv, timeout_s)


@pytest.mark.parametrize("args", [{}, {"paths": "  "}])
async def test_run_tests_without_paths_rejected(tmp_path, args):
    sandbox = ExecSpySandbox(tmp_path)
    box = ToolBox(
        sandbox,
        AgentSpec(
            test_argv=ECHO_ARGV,
            test_timeout_s=3600.0,
            narrow_test_timeout_s=120.0,
        ),
    )
    with pytest.raises(ToolError, match="paths обязателен"):
        await box.call(TOOL_RUN_TESTS, args)
    assert sandbox.exec_calls == 0


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
    from sa_home_bot.devlab.agent.loop import STEP_REJECTED

    assert "исправь и вызови" in FINISH_REJECTED
    assert "исправь и вызови" in STEP_REJECTED


class GateSandbox(LocalSandbox):
    def __init__(self, root: Path, state: dict) -> None:
        super().__init__(root)
        self._state = state

    async def exec(self, argv, timeout_s):
        self._state["now"] += 1
        self._state["max"] = max(self._state["max"], self._state["now"])
        try:
            await asyncio.sleep(0.05)
        finally:
            self._state["now"] -= 1
        return ExecResult(0, "ok")


@pytest.mark.parametrize("shared", [True, False])
async def test_full_runs_between_slots_go_by_turn(tmp_path, shared):
    state = {"now": 0, "max": 0}
    lock = asyncio.Lock() if shared else None
    guards = [
        FinishGuard(sandbox, AgentSpec(test_argv=GREEN_TEST), lock)
        for sandbox in (GateSandbox(tmp_path, state) for _ in range(2))
    ]
    await asyncio.gather(*(guard.verify_green() for guard in guards))
    assert state["max"] == (1 if shared else 2)


async def test_trace_has_one_entry_per_step_and_cuts_long_arguments(tmp_path):
    entries = []
    long_content = "SECRET" * 200
    model = ScriptedChatModel(
        tool_reply(TOOL_WRITE_FILE, "w1", path="src/new.py", content=long_content),
        edit("c2", old="NO SUCH TEXT"),
        edit("c3"),
        tool_reply(TOOL_FINISH, "f1", summary="готово"),
    )
    ticks = iter(range(1000))
    loop, _ = make_loop(tmp_path, model, trace=entries.append, clock=lambda: next(ticks) * 0.375)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED
    steps = [entry for entry in entries if "step" in entry]
    assert [entry["step"] for entry in steps] == [1, 2, 3, 4]
    assert {entry["model_s"] for entry in steps} == {0.38}
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


@pytest.mark.parametrize(
    ("code", "failed"), [("print('ok')", False), ("raise ValueError('нет')", True)]
)
async def test_trace_notes_execute_code_size_and_failure(tmp_path, monkeypatch, code, failed):
    monkeypatch.setattr(
        tools_module,
        "worker_argv",
        lambda config: worker_argv(config, python=sys.executable),
    )
    entries = []
    spec = AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",), code_mode=True)
    model = ScriptedChatModel(tool_reply(TOOL_EXECUTE, "s1", code=code))
    loop, _ = make_loop(
        tmp_path, model, spec=spec, limits=AgentLimits(max_steps=1), trace=entries.append
    )
    await loop.run("правила", "задача")
    call = next(entry for entry in entries if "step" in entry)["calls"][0]
    assert call["code_chars"] == len(code)
    assert call["script_failed"] is failed


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


def user_texts(messages):
    return [m["content"] for m in messages if m["role"] == "user"]


def assert_pairs_intact(messages):
    asked = [
        c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls") or ()
    ]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert answered == asked


async def test_repeat_is_collapsed_into_turn_message(tmp_path):
    model = ScriptedChatModel(
        lint_call("a"), lint_call("b"), edit(), tool_reply(TOOL_FINISH, summary="ok")
    )
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.FINISHED
    messages = model.seen[-1]
    assert REPEAT_TURN.format(number=1) in user_texts(messages)
    assert_pairs_intact(messages)
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"][0] == "a"


async def test_repeat_crane_after_turn_is_ignored(tmp_path):
    entries = []
    model = ScriptedChatModel(*(lint_call(c) for c in "abcde"), lint_call("f"))
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec(), trace=entries.append)
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.ASKED
    assert result.crane is Crane.REPEAT
    assert result.steps == 5
    assert result.reason == "модель повторяет run_command echo x.py"
    assert result.dialog.call_id is None
    messages = result.dialog.messages
    tool_results = [m["content"] for m in messages if m["role"] == "tool"]
    assert tool_results[-1] == REPEAT_SKIPPED
    assert len(tool_results) == 2
    assert user_texts(messages).count(REPEAT_TURN.format(number=1)) == 1
    assert_pairs_intact(messages)
    assert entries[-1]["event"] == "end"
    assert entries[-1]["crane"] == "repeat"
    assert len([e for e in entries if "step" in e]) == 5


async def test_repeated_read_is_collapsed_then_crane(tmp_path):
    model = ScriptedChatModel(*(tool_reply(TOOL_READ_FILE, c, path="src/app.py") for c in "abcde"))
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    result = await loop.run("правила", "задача")
    assert result.crane is Crane.REPEAT
    assert result.steps == 5
    assert CODE_REPEAT_TURN.format(number=1) not in user_texts(result.dialog.messages)
    assert REPEAT_TURN.format(number=1) in user_texts(result.dialog.messages)


def execute_spec():
    return AgentSpec(test_argv=GREEN_TEST, diff_prefixes=("src/",), code_mode=True)


@pytest.mark.parametrize(
    ("codes", "steps"),
    [
        (["print(tr[5:9])"] * 5, 5),
        ([f"print(json.dumps(tr[{i}:{i + 4}]))" for i in range(5, 11)], 6),
    ],
)
async def test_repeat_chains_hit_crane_at_their_threshold(tmp_path, codes, steps):
    model = ScriptedChatModel(
        *(tool_reply(TOOL_EXECUTE, str(i), code=code) for i, code in enumerate(codes))
    )
    loop, _ = make_loop(tmp_path, model, spec=execute_spec())
    result = await loop.run("правила", "задача")
    assert result.crane is Crane.REPEAT
    assert result.steps == steps
    assert CODE_REPEAT_TURN.format(number=1) in user_texts(result.dialog.messages)


@pytest.mark.parametrize(
    "codes",
    [
        [
            f"print(open('{name}').read())"
            for name in ("src/a.py", "src/b.py", "docs/c.md", "tests/d.py", "x.toml")
        ],
        [f"v{i} = {i}\nprint(v{i})" if i % 2 else f"print('{'z' * i}')" for i in range(1, 6)],
        [f"print(read('src/a.py', {i * 150 + 1}, {i * 150 + 150}))" for i in range(5)],
    ],
)
async def test_different_code_is_not_a_repeat(tmp_path, codes):
    model = ScriptedChatModel(
        *(tool_reply(TOOL_EXECUTE, str(i), code=code) for i, code in enumerate(codes)),
        ModelError("конец"),
    )
    loop, _ = make_loop(tmp_path, model, spec=execute_spec())
    result = await loop.run("правила", "задача")
    assert result.status is AgentStatus.MODEL_FAILED
    assert result.crane is None


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
    model = ScriptedChatModel(*(lint_call(str(i)) for i in range(10)))
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    first = await loop.run("правила", "задача")
    assert first.crane is Crane.REPEAT
    answered = Dialog(messages=first.dialog.messages, question="q", answer="продолжай")
    second = await loop.run("правила", "задача", resume=answered)
    assert second.crane is Crane.REPEAT
    assert second.steps == 5


async def test_last_repeat_skips_siblings_without_dangling_call_ids(tmp_path):
    batch = ModelReply(
        "",
        (
            ToolCall("s1", TOOL_READ_FILE, json.dumps({"path": "src/app.py"})),
            ToolCall("s2", TOOL_RUN_COMMAND, json.dumps({"command": "echo x.py"})),
            ToolCall("s3", TOOL_READ_FILE, json.dumps({"path": "src/app.py"})),
        ),
    )
    model = ScriptedChatModel(*(lint_call(c) for c in "abcd"), batch)
    loop, _ = make_loop(tmp_path, model, spec=repeat_spec())
    result = await loop.run("правила", "задача")
    assert result.crane is Crane.REPEAT
    messages = result.dialog.messages
    assert_pairs_intact(messages)
    results = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    assert results["s2"] == REPEAT_SKIPPED
    assert results["s3"] == REPEAT_SKIPPED


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


BASE_RED_ID = "tests/other_test.py::test_other"
BASE_RED_OUT = f"FAILED {BASE_RED_ID} - assert 0\n1 failed, 3 passed in 1s"


class BaseRedSandbox(LocalSandbox):
    def __init__(self, root: Path, base_out: str = BASE_RED_OUT, test_out: str = BASE_RED_OUT):
        super().__init__(root)
        self.base_out = base_out
        self.test_out = test_out
        self.base_runs = 0

    async def exec(self, argv, timeout_s):
        return ExecResult(1, self.test_out)

    async def exec_on_base(self, argv, timeout_s):
        self.base_runs += 1
        return ExecResult(1, self.base_out)


async def test_finish_guard_excuses_tests_red_on_base(tmp_path):
    sandbox = BaseRedSandbox(tmp_path)
    guard = FinishGuard(sandbox, AgentSpec(test_argv=("pytest", "-n", "auto")))
    assert await guard.verify_green() == []
    assert guard.excused == [BASE_RED_ID]
    assert await guard.verify_green() == []
    assert sandbox.base_runs == 1


SPACED_ID = "tests/unit/test_vpn.py::test_phone[\\U0001f4f1 Rose]"


@pytest.mark.parametrize(
    "output,expected",
    [
        (f"FAILED {SPACED_ID} - AssertionError: x - y\n", [SPACED_ID]),
        (f"ERROR {SPACED_ID}\n", [SPACED_ID]),
        (f"FAILED {BASE_RED_ID}   \n", [BASE_RED_ID]),
        (f"FAILED {BASE_RED_ID} - assert 0\nFAILED {BASE_RED_ID} - assert 0\n", [BASE_RED_ID]),
    ],
)
def test_failed_test_ids_keep_spaces_inside_parameters(output, expected):
    assert failed_test_ids(output) == expected


async def test_finish_guard_excuses_base_red_test_with_spaced_id(tmp_path):
    out = f"FAILED {SPACED_ID} - assert 0\n1 failed, 3 passed in 1s"
    sandbox = BaseRedSandbox(tmp_path, base_out=out, test_out=out)
    guard = FinishGuard(sandbox, AgentSpec(test_argv=("pytest",)))
    assert await guard.verify_green() == []
    assert guard.excused == [SPACED_ID]


async def test_finish_guard_blocks_tests_green_on_base(tmp_path):
    sandbox = BaseRedSandbox(tmp_path, base_out="1 passed in 1s")
    guard = FinishGuard(sandbox, AgentSpec(test_argv=("pytest",)))
    failures = await guard.verify_green()
    assert failures and "тесты не зелёные" in failures[0]
    assert guard.excused == []


async def test_finish_guard_does_not_excuse_changed_test_file(tmp_path):
    (tmp_path / "tests").mkdir()
    sandbox = BaseRedSandbox(tmp_path)
    (tmp_path / "tests/other_test.py").write_text("x = 1\n")
    guard = FinishGuard(sandbox, AgentSpec(test_argv=("pytest",)))
    assert await guard.verify_green()
    assert sandbox.base_runs == 0


async def test_finish_guard_blocks_when_failure_count_unparsed(tmp_path):
    out = f"FAILED {BASE_RED_ID}\n2 failed in 1s"
    guard = FinishGuard(BaseRedSandbox(tmp_path, test_out=out), AgentSpec(test_argv=("pytest",)))
    assert await guard.verify_green()


async def test_finish_reports_base_red_in_summary(tmp_path):
    sandbox = BaseRedSandbox(tmp_path)
    spec = AgentSpec(test_argv=("pytest",), diff_prefixes=("src/",))
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("VALUE = 1\n")
    loop = AgentLoop(
        ScriptedChatModel(edit(), tool_reply(TOOL_FINISH, summary="готово")),
        ToolBox(sandbox, spec),
        FinishGuard(sandbox, spec),
    )
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.FINISHED
    assert result.summary.startswith("готово")
    assert f"Красные на базе (не блокировали финиш): {BASE_RED_ID}" in result.summary


async def test_repeated_finish_rejection_asks_owner(tmp_path):
    spec = AgentSpec(test_argv=RED_TEST, diff_prefixes=("src/",))
    finishes = [tool_reply(TOOL_FINISH, f"f{n}", summary="готово") for n in range(3)]
    model = ScriptedChatModel(edit(), *finishes)
    loop, _ = make_loop(tmp_path, model, spec=spec)
    result = await loop.run("s", "t")
    assert result.status is AgentStatus.ASKED
    assert result.crane is None
    assert "Финиш отклонён 3 раза подряд" in result.reason
    assert "boom" in result.reason


async def test_finish_rejection_counter_resets_on_different_failure(tmp_path):
    spec = AgentSpec(test_argv=RED_TEST, diff_prefixes=("src/",))
    model = ScriptedChatModel(
        edit(),
        tool_reply(TOOL_FINISH, "f0", summary="готово"),
        tool_reply(TOOL_FINISH, "f1", summary="готово"),
        tool_reply(TOOL_RUN_TESTS, "t", paths="tests/x.py"),
    )
    loop, _ = make_loop(tmp_path, model, spec=spec, limits=AgentLimits(max_steps=4))
    result = await loop.run("s", "t")
    assert result.crane is Crane.STEPS
