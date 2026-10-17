from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError
from sa_home_bot.utils.http import JSON_FETCH_ERRORS, get_json_async

log = logging.getLogger(__name__)

_VERSION_PROBE_TIMEOUT_S = 3.0
_WARMUP_POLL_INTERVAL_S = 2.0
_WARMUP_TIMEOUT_S = 150.0
_POST_RETRY_ATTEMPTS = 3
_POST_RETRY_DELAY_S = 3.0


def _append_error_body(exc: urllib.error.HTTPError) -> None:
    detail = exc.read().decode("utf-8", "replace").strip()
    with contextlib.suppress(ValueError):
        parsed = json.loads(detail)
        if isinstance(parsed, dict) and parsed.get("error"):
            detail = str(parsed["error"])
    if detail:
        exc.msg = f"{exc.msg}: {detail}"


def parse_json_object(content: str) -> dict[str, Any]:
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("ответ не объект")
    return data


def _json_post_request(url: str, payload: dict[str, Any]) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )


def _post_json_sync(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(_json_post_request(url, payload), timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        _append_error_body(exc)
        raise


def _post_json_stream_sync(
    url: str,
    payload: dict[str, Any],
    timeout: float,
    on_chunk: Callable[[str], None],
) -> dict[str, Any]:
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    final: dict[str, Any] = {}
    try:
        with urllib.request.urlopen(_json_post_request(url, payload), timeout=timeout) as resp:  # noqa: S310
            for raw_line in resp:
                line = raw_line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                thought = (chunk.get("message") or {}).get("thinking", "")
                if thought:
                    thinking_parts.append(thought)
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    content_parts.append(piece)
                    on_chunk("".join(content_parts))
                if chunk.get("done"):
                    final = chunk
    except urllib.error.HTTPError as exc:
        _append_error_body(exc)
        raise
    final_message = dict(final.get("message") or {})
    final_message["content"] = "".join(content_parts)
    if thinking_parts:
        final_message["thinking"] = "".join(thinking_parts)
    final["message"] = final_message
    return final


async def ollama_version(
    cfg: LlmConfig, timeout: float = _VERSION_PROBE_TIMEOUT_S
) -> dict[str, Any] | None:
    try:
        return await get_json_async(f"{cfg.ollama_url}/api/version", timeout)
    except JSON_FETCH_ERRORS:
        return None


def _wsl_argv(cfg: LlmConfig, *args: str) -> list[str]:
    return ["wsl", "-d", cfg.wsl_distro, "-u", "root", "--", *args]


class WslKeepalive:
    def __init__(self, cfg: LlmConfig, duration_s: float) -> None:
        self._cfg = cfg
        self._duration_s = duration_s
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *_wsl_argv(self._cfg, "sleep", str(int(self._duration_s))),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("llm: не удалось запустить keepalive-процесс WSL: %s", exc)
            self._proc = None

    async def stop(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(ProcessLookupError):
            self._proc.terminate()
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        self._proc = None


async def _wsl_exec(cfg: LlmConfig, *args: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            *_wsl_argv(cfg, *args),
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
    if await ollama_version(cfg) is not None:
        return
    if cfg.container_backend == "native":
        log.info("llm: Ollama (native) не отвечает — жду поднятия systemd-сервиса")
    else:
        log.info("llm: Ollama не отвечает — прогрев WSL/контейнера %s", cfg.ollama_container)
        await _wsl_exec(cfg, "true")
        await _wsl_exec(cfg, "docker", "start", cfg.ollama_container)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WARMUP_TIMEOUT_S
    while loop.time() < deadline:
        if await ollama_version(cfg) is not None:
            return
        await asyncio.sleep(_WARMUP_POLL_INTERVAL_S)
    raise ProtoError(ERR_INTERNAL, "Ollama не поднялась после прогрева WSL/контейнера")


async def stop(cfg: LlmConfig) -> None:
    if cfg.container_backend == "native":
        payload = {"model": cfg.model, "prompt": "", "stream": False, "keep_alive": 0}
        try:
            await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)
        except ProtoError:
            log.warning("llm: не удалось явно выгрузить модель (keep_alive=0)")
        return
    await _wsl_exec(cfg, "docker", "stop", cfg.ollama_container)


def _is_transient_connection_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError))


async def _post_with_retry(
    cfg: LlmConfig,
    url: str,
    payload: dict[str, Any],
    on_chunk: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    streaming = on_chunk is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.request_timeout_s
    last_exc: Exception | None = None
    for attempt in range(1, _POST_RETRY_ATTEMPTS + 1):
        await ensure_running(cfg)
        left = deadline - loop.time()
        if left <= 0:
            break
        sent_any = False

        def _mark_chunk(text: str) -> None:
            nonlocal sent_any
            sent_any = True
            on_chunk(text)

        try:
            if streaming:
                return await asyncio.to_thread(
                    _post_json_stream_sync, url, payload, left, _mark_chunk
                )
            return await asyncio.to_thread(_post_json_sync, url, payload, left)
        except JSON_FETCH_ERRORS as exc:
            last_exc = exc
            out_of_attempts = attempt >= _POST_RETRY_ATTEMPTS
            if sent_any or out_of_attempts or not _is_transient_connection_error(exc):
                break
            if deadline - loop.time() <= _POST_RETRY_DELAY_S:
                break
            log.warning(
                "llm: %s%s оборвался сразу после прогрева (%s) — повтор через %.0fс "
                "(попытка %d/%d)",
                url,
                " (стрим)" if streaming else "",
                exc,
                _POST_RETRY_DELAY_S,
                attempt,
                _POST_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(_POST_RETRY_DELAY_S)
    if last_exc is None:
        raise ProtoError(
            ERR_INTERNAL, f"{url}: бюджет {cfg.request_timeout_s:.0f}с истёк на прогреве"
        )
    raise ProtoError(ERR_INTERNAL, f"{url}: {last_exc}")


def _keep_alive_options(cfg: LlmConfig) -> dict[str, Any]:
    return {"keep_alive": cfg.idle_sleep_after_s, "options": {"num_ctx": cfg.num_ctx}}


async def preload(cfg: LlmConfig) -> None:
    payload = {"model": cfg.model, "prompt": "", "stream": False, **_keep_alive_options(cfg)}
    await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)


async def generate(cfg: LlmConfig, prompt: str, system: str) -> dict[str, Any]:
    payload = {
        "model": cfg.model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "think": False,
        **_keep_alive_options(cfg),
    }
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/generate", payload)


async def chat(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
    response_format: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    full_messages = [{"role": "system", "content": system}, *messages]
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": full_messages,
        "stream": False,
        **_keep_alive_options(cfg),
    }
    if options:
        payload["options"] = {**options, **payload["options"]}
    if response_format is not None:
        payload["format"] = response_format
    if think is not None:
        payload["think"] = think
    if tools:
        payload["tools"] = tools
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/chat", payload)


async def chat_stream(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    on_chunk: Callable[[str], None],
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
) -> dict[str, Any]:
    full_messages = [{"role": "system", "content": system}, *messages]
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": full_messages,
        "stream": True,
        **_keep_alive_options(cfg),
    }
    if think is not None:
        payload["think"] = think
    if tools:
        payload["tools"] = tools
    return await _post_with_retry(cfg, f"{cfg.ollama_url}/api/chat", payload, on_chunk)
