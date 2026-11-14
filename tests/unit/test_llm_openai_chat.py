from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import openai_chat
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError

CHAT_ROUTE = "/v1/chat/completions"


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(openai_chat, "_POST_RETRY_DELAY_S", 0.01)


def test_to_openai_messages_puts_system_first_and_copies_plain_messages():
    out = openai_chat.to_openai_messages(
        [
            {"role": "user", "content": "привет"},
            {"role": "assistant", "content": "доброго дня"},
            {"role": "system", "content": "уточнение роутера"},
        ],
        "персона",
    )
    assert out == [
        {"role": "system", "content": "персона"},
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "доброго дня"},
        {"role": "system", "content": "уточнение роутера"},
    ]


def test_to_openai_messages_splits_image_into_content_parts():
    out = openai_chat.to_openai_messages(
        [
            {"role": "user", "content": "что на снимке?", "images": ["QUJD"]},
            {"role": "user", "images": ["REZ1", "REZ2"]},
        ],
        "персона",
    )
    assert out[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "что на снимке?"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        ],
    }
    assert out[2]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,REZ1"}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,REZ2"}},
    ]


def test_to_openai_messages_numbers_tool_calls_and_links_tool_answers():
    out = openai_chat.to_openai_messages(
        [
            {"role": "user", "content": "напомни и найди"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": "remind", "arguments": {"text": "раз"}}},
                    {"function": {"name": "web_search", "arguments": {"query": "поиск"}}},
                ],
            },
            {"role": "tool", "content": "ок", "name": "remind"},
            {"role": "tool", "content": "результат", "name": "web_search"},
            {
                "role": "assistant",
                "content": "уже делаю",
                "tool_calls": [{"function": {"name": "remind", "arguments": {}}}],
            },
            {"role": "tool", "content": "готово"},
        ],
        "персона",
    )
    assert out[2] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "remind", "arguments": '{"text": "раз"}'},
            },
            {
                "id": "call_2",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query": "поиск"}'},
            },
        ],
    }
    assert out[3] == {"role": "tool", "content": "ок", "tool_call_id": "call_1", "name": "remind"}
    assert out[4] == {
        "role": "tool",
        "content": "результат",
        "tool_call_id": "call_2",
        "name": "web_search",
    }
    assert out[5]["tool_calls"][0]["id"] == "call_3"
    assert out[5]["content"] == "уже делаю"
    assert out[6] == {"role": "tool", "content": "готово", "tool_call_id": "call_3"}


def test_to_openai_messages_keeps_existing_tool_call_id_and_advances_numbering():
    out = openai_chat.to_openai_messages(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_7", "function": {"name": "remind", "arguments": "{}"}},
                    {"function": {"name": "web_search", "arguments": {"query": "q"}}},
                ],
            },
        ],
        "персона",
    )
    assert [call["id"] for call in out[1]["tool_calls"]] == ["call_7", "call_2"]
    assert out[1]["tool_calls"][0]["function"]["arguments"] == "{}"


@pytest.mark.parametrize(
    ("think", "expected"),
    [
        (None, {}),
        (False, {"chat_template_kwargs": {"enable_thinking": False}}),
        ("off", {"chat_template_kwargs": {"enable_thinking": False}}),
        (True, {"reasoning_budget_tokens": 8192}),
        ("high", {"reasoning_budget_tokens": 8192}),
        ("medium", {"reasoning_budget_tokens": 4096}),
        ("low", {"reasoning_budget_tokens": 1024}),
    ],
)
def test_think_fields(think, expected):
    assert openai_chat.think_fields(think) == expected


def test_options_fields_maps_num_predict_and_drops_the_rest():
    fields = openai_chat.options_fields(
        {"temperature": 0, "top_p": 0.9, "seed": 17, "num_predict": 512, "num_ctx": 8192}
    )
    assert fields == {"temperature": 0, "top_p": 0.9, "seed": 17, "max_tokens": 512}
    assert openai_chat.options_fields(None) == {}
    assert openai_chat.options_fields({"num_ctx": 8192}) == {}


def openai_reply(content="ответ", reasoning=None, calls=None, finish="stop", model="qwen3.8"):
    message: dict[str, object] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    if calls is not None:
        message["tool_calls"] = calls
    return {
        "model": model,
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4},
    }


@pytest.mark.parametrize(
    ("raw", "expected_message", "expected_done_reason"),
    [
        (openai_reply("Доброго дня"), {"role": "assistant", "content": "Доброго дня"}, "stop"),
        (
            openai_reply("итог", reasoning="  план  "),
            {"role": "assistant", "content": "итог", "thinking": "  план  "},
            "stop",
        ),
        (
            openai_reply("", reasoning=""),
            {"role": "assistant", "content": ""},
            "stop",
        ),
        (
            openai_reply(
                "",
                calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "remind", "arguments": '{"text":"раз"}'},
                    }
                ],
                finish="tool_calls",
            ),
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "remind", "arguments": {"text": "раз"}}}],
            },
            "stop",
        ),
        (
            openai_reply(
                "обрезано",
                calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "remind", "arguments": "не json"},
                    }
                ],
                finish="length",
            ),
            {
                "role": "assistant",
                "content": "обрезано",
                "tool_calls": [{"function": {"name": "remind", "arguments": {}}}],
            },
            "length",
        ),
    ],
)
def test_from_openai_response(raw, expected_message, expected_done_reason):
    result = openai_chat.from_openai_response(raw, "qwen3.8", 2.5)
    assert result["message"] == expected_message
    assert result["done_reason"] == expected_done_reason
    assert result["model"] == "qwen3.8"
    assert result["prompt_eval_count"] == 11
    assert result["eval_count"] == 4
    assert result["total_duration"] == 2_500_000_000


def test_from_openai_response_without_usage_and_choices():
    result = openai_chat.from_openai_response({}, "qwen3.8", 0.5)
    assert result == {
        "model": "qwen3.8",
        "message": {"role": "assistant", "content": ""},
        "done_reason": "stop",
        "prompt_eval_count": 0,
        "eval_count": 0,
        "total_duration": 500_000_000,
    }


async def run_server(handler, **cfg_overrides):
    app = web.Application()
    app.router.add_post(CHAT_ROUTE, handler)
    server = TestServer(app)
    await server.start_server()
    cfg = LlmConfig(
        model="qwen3.8",
        backend="openai",
        openai_base_url=str(server.make_url("/v1")),
        request_timeout_s=5.0,
        **cfg_overrides,
    )
    return server, cfg


def openai_handler(seen, reply=None, fail_first=False):
    async def handler(request):
        seen.setdefault("bodies", []).append(await request.json())
        seen.setdefault("headers", []).append(dict(request.headers))
        if fail_first and len(seen["bodies"]) == 1:
            request.transport.close()
            return web.json_response({})
        return web.json_response(reply or openai_reply("Доброго дня"))

    return handler


async def test_chat_posts_openai_body_and_parses_reply(tmp_path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("  secret-key-42  \n")
    seen: dict[str, list] = {}
    server, cfg = await run_server(openai_handler(seen), openai_api_key_file=key_file)
    try:
        result = await openai_chat.chat(
            cfg,
            [{"role": "user", "content": "привет"}],
            "персона",
            tools=[{"type": "function", "function": {"name": "remind"}}],
            think="low",
            response_format="json",
            options={"temperature": 0, "num_predict": 512, "num_ctx": 8192},
        )
    finally:
        await server.close()

    body = seen["bodies"][0]
    assert body["model"] == "qwen3.8"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "персона"},
        {"role": "user", "content": "привет"},
    ]
    assert body["tools"] == [{"type": "function", "function": {"name": "remind"}}]
    assert body["reasoning_budget_tokens"] == 1024
    assert body["temperature"] == 0
    assert body["max_tokens"] == 512
    assert "num_ctx" not in body
    assert "response_format" not in body and "format" not in body
    assert seen["headers"][0]["Authorization"] == "Bearer secret-key-42"
    assert result["message"]["content"] == "Доброго дня"
    assert result["done_reason"] == "stop"
    assert result["prompt_eval_count"] == 11


async def test_chat_without_key_file_sends_no_authorization():
    seen: dict[str, list] = {}
    server, cfg = await run_server(openai_handler(seen))
    try:
        await openai_chat.chat(cfg, [{"role": "user", "content": "привет"}], "персона")
    finally:
        await server.close()
    assert "Authorization" not in seen["headers"][0]


async def test_generate_sends_single_user_message_and_disables_thinking():
    seen: dict[str, list] = {}
    server, cfg = await run_server(openai_handler(seen))
    try:
        result = await openai_chat.generate(cfg, "привет", "персона")
    finally:
        await server.close()
    body = seen["bodies"][0]
    assert body["messages"] == [
        {"role": "system", "content": "персона"},
        {"role": "user", "content": "привет"},
    ]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in body
    assert result["message"]["content"] == "Доброго дня"


async def test_chat_retries_after_connection_drop():
    seen: dict[str, list] = {}
    server, cfg = await run_server(openai_handler(seen, fail_first=True))
    try:
        result = await openai_chat.chat(cfg, [{"role": "user", "content": "привет"}], "персона")
    finally:
        await server.close()
    assert len(seen["bodies"]) == 2
    assert result["message"]["content"] == "Доброго дня"


async def test_chat_http_error_keeps_body_and_hides_key(tmp_path):
    key_file = tmp_path / "key.txt"
    key_file.write_text("secret-key-42")

    async def handler(request):
        return web.json_response({"error": "модель не найдена"}, status=500)

    server, cfg = await run_server(handler, openai_api_key_file=key_file)
    try:
        with pytest.raises(ProtoError) as excinfo:
            await openai_chat.chat(cfg, [{"role": "user", "content": "привет"}], "персона")
    finally:
        await server.close()
    assert excinfo.value.code == ERR_INTERNAL
    assert "модель не найдена" in str(excinfo.value)
    assert "secret-key-42" not in str(excinfo.value)
