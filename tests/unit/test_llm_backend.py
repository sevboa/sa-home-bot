from __future__ import annotations

import pytest

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import backend, ollama, openai_chat

MODEL_CALLS = ("chat", "chat_stream", "generate", "preload", "ensure_running", "stop")


def cfg_for(transport: str) -> LlmConfig:
    return LlmConfig(model="qwen3.8", backend=transport, request_timeout_s=5.0)


def _sink(text: str) -> None:
    pass


@pytest.fixture
def journal(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake(module_label: str, func_name: str):
        async def run(*args, **kwargs):
            calls.append({"transport": module_label, "func": func_name, "kwargs": kwargs})
            return {"transport": module_label, "func": func_name}

        return run

    for module_label, module in (("ollama", ollama), ("openai", openai_chat)):
        for func_name in MODEL_CALLS:
            if hasattr(module, func_name):
                monkeypatch.setattr(module, func_name, fake(module_label, func_name))

    return calls


@pytest.mark.parametrize(
    ("func_name", "call"),
    [
        ("chat", lambda cfg: backend.chat(cfg, [{"role": "user", "content": "1"}], "система")),
        (
            "chat_stream",
            lambda cfg: backend.chat_stream(cfg, [{"role": "user", "content": "1"}], "с", _sink),
        ),
        ("generate", lambda cfg: backend.generate(cfg, "вопрос", "система")),
    ],
)
async def test_transport_is_picked_per_call(func_name, call, journal):
    await call(cfg_for("ollama"))
    await call(cfg_for("openai"))
    assert [entry["func"] for entry in journal] == [func_name, func_name]
    assert [entry["transport"] for entry in journal] == ["ollama", "openai"]


async def test_openai_backend_calls_nothing_for_warmup_and_sleep(journal):
    await backend.preload(cfg_for("openai"))
    await backend.ensure_running(cfg_for("openai"))
    await backend.stop(cfg_for("openai"))
    assert journal == []


async def test_ollama_backend_warms_up_and_sleeps_ollama(journal):
    await backend.preload(cfg_for("ollama"))
    await backend.ensure_running(cfg_for("ollama"))
    await backend.stop(cfg_for("ollama"))
    assert [entry["func"] for entry in journal] == ["preload", "ensure_running", "stop"]
    assert {entry["transport"] for entry in journal} == {"ollama"}


async def test_chat_forwards_keyword_arguments_to_the_transport(journal):
    await backend.chat(
        cfg_for("openai"),
        [{"role": "user", "content": "1"}],
        "система",
        tools=[{"type": "function"}],
        think="low",
        response_format="json",
        options={"temperature": 0},
    )
    assert journal[0]["kwargs"] == {
        "tools": [{"type": "function"}],
        "think": "low",
        "response_format": "json",
        "options": {"temperature": 0},
    }


async def test_chat_stream_forwards_tools_and_think(journal):
    await backend.chat_stream(
        cfg_for("openai"),
        [{"role": "user", "content": "1"}],
        "система",
        _sink,
        tools=[],
        think=False,
    )
    assert journal[0]["kwargs"] == {"tools": [], "think": False}
