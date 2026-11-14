from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm import ollama, openai_chat

_TRANSPORTS = {"ollama": ollama, "openai": openai_chat}


def _transport(cfg: LlmConfig):
    return _TRANSPORTS[cfg.backend]


def _without_none(values: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in values.items() if value is not None}


async def chat(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
    response_format: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _transport(cfg).chat(
        cfg,
        messages,
        system,
        **_without_none(
            {
                "tools": tools,
                "think": think,
                "response_format": response_format,
                "options": options,
            }
        ),
    )


async def chat_stream(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    on_chunk: Callable[[str], None],
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
) -> dict[str, Any]:
    return await _transport(cfg).chat_stream(
        cfg, messages, system, on_chunk, **_without_none({"tools": tools, "think": think})
    )


async def generate(cfg: LlmConfig, prompt: str, system: str) -> dict[str, Any]:
    return await _transport(cfg).generate(cfg, prompt, system)


async def preload(cfg: LlmConfig) -> None:
    if cfg.backend == "openai":
        return
    await ollama.preload(cfg)


async def ensure_running(cfg: LlmConfig) -> None:
    if cfg.backend == "openai":
        return
    await ollama.ensure_running(cfg)


async def stop(cfg: LlmConfig) -> None:
    if cfg.backend == "openai":
        return
    await ollama.stop(cfg)
