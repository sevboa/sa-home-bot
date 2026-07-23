"""Тонкий клиент Ollama (loopback, только localhost) + прогрев/усыпление
контейнера внутри WSL2 на этой же машине (см. LLM_INTEGRATION_PLAN.md §0-1).

Никакого httpx/aiohttp — проект сознательно не тянет HTTP-библиотеку ради
одной локальной службы (urllib + asyncio.to_thread хватает для loopback,
см. pyproject.toml).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError

log = logging.getLogger(__name__)

_VERSION_PROBE_TIMEOUT_S = 3.0
_WARMUP_POLL_INTERVAL_S = 2.0
_WARMUP_TIMEOUT_S = 90.0


def _post_json_sync(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — только localhost
        return json.loads(resp.read())


def _get_json_sync(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — только localhost
        return json.loads(resp.read())


async def ollama_version(
    cfg: LlmConfig, timeout: float = _VERSION_PROBE_TIMEOUT_S
) -> dict[str, Any] | None:
    """None — Ollama сейчас не отвечает (контейнер спит/WSL не поднята)."""
    try:
        return await asyncio.to_thread(_get_json_sync, f"{cfg.ollama_url}/api/version", timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


async def _wsl_exec(cfg: LlmConfig, *args: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "wsl",
            "-d",
            cfg.wsl_distro,
            "-u",
            "root",
            "--",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_raw = await proc.communicate()
    except OSError as exc:
        log.warning("llm: не удалось выполнить wsl %s: %s", args, exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "llm: wsl %s завершился кодом %s: %s",
            args,
            proc.returncode,
            stderr_raw.decode(errors="replace").strip(),
        )
        return False
    return True


async def ensure_running(cfg: LlmConfig) -> None:
    """Прогреть WSL/контейнер, если Ollama сейчас не отвечает.

    Дёшево, если уже поднята: один быстрый `/api/version` и выход. Ждёт
    здесь же, синхронно с вызовом `ask`/`chat` — таймаут на это отдельный
    (`_WARMUP_TIMEOUT_S`, чисто внутренний, локальный), протокольный таймаут
    роя (`Envelope.timeout_s`) считается от начала запроса бота и покрывает
    в том числе и этот прогрев.
    """
    if await ollama_version(cfg) is not None:
        return
    log.info("llm: Ollama не отвечает — прогрев WSL/контейнера %s", cfg.ollama_container)
    await _wsl_exec(cfg, "true")  # поднять сам дистрибутив WSL
    await _wsl_exec(cfg, "docker", "start", cfg.ollama_container)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WARMUP_TIMEOUT_S
    while loop.time() < deadline:
        if await ollama_version(cfg) is not None:
            return
        await asyncio.sleep(_WARMUP_POLL_INTERVAL_S)
    raise ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева WSL/контейнера")


async def stop(cfg: LlmConfig) -> None:
    await _wsl_exec(cfg, "docker", "stop", cfg.ollama_container)


async def generate(cfg: LlmConfig, prompt: str, system: str) -> dict[str, Any]:
    await ensure_running(cfg)
    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": False,
    }
    try:
        return await asyncio.to_thread(
            _post_json_sync, f"{cfg.ollama_url}/api/generate", payload, cfg.request_timeout_s
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProtoError(ERR_INTERNAL, f"Ollama /api/generate: {exc}") from exc


async def chat(cfg: LlmConfig, messages: list[dict[str, str]], system: str) -> dict[str, Any]:
    await ensure_running(cfg)
    full_messages = [{"role": "system", "content": system}, *messages]
    payload = {"model": cfg.model, "messages": full_messages, "stream": False, "think": False}
    try:
        return await asyncio.to_thread(
            _post_json_sync, f"{cfg.ollama_url}/api/chat", payload, cfg.request_timeout_s
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProtoError(ERR_INTERNAL, f"Ollama /api/chat: {exc}") from exc
