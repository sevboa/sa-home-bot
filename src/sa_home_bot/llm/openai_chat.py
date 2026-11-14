from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sa_home_bot.config import LlmConfig
from sa_home_bot.llm.ollama import _append_error_body, _is_transient_connection_error
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError
from sa_home_bot.utils.http import JSON_FETCH_ERRORS

log = logging.getLogger(__name__)

_POST_RETRY_ATTEMPTS = 3
_POST_RETRY_DELAY_S = 3.0

CHAT_COMPLETIONS_PATH = "/chat/completions"
_THINK_BUDGET_HIGH_TOKENS = 8192
_THINK_BUDGET_MEDIUM_TOKENS = 4096
_THINK_BUDGET_LOW_TOKENS = 1024
_THINK_BUDGET_TOKENS: dict[str, int] = {
    "high": _THINK_BUDGET_HIGH_TOKENS,
    "medium": _THINK_BUDGET_MEDIUM_TOKENS,
    "low": _THINK_BUDGET_LOW_TOKENS,
}
_SAMPLING_FIELDS: tuple[str, ...] = ("temperature", "top_p", "seed")
_TOOL_CALL_ID_PREFIX = "call_"
_IMAGE_URL_PREFIX = "data:image/jpeg;base64,"
_FINISH_LENGTH = "length"


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(image_b64: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"{_IMAGE_URL_PREFIX}{image_b64}"}}


def _openai_tool_call(raw: dict[str, Any], call_id: str) -> dict[str, Any]:
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    arguments = function.get("arguments")
    if not isinstance(arguments, (dict, str)):
        arguments = {}
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": arguments
            if isinstance(arguments, str)
            else json.dumps(arguments, ensure_ascii=False),
        },
    }


def _assistant_message(
    message: dict[str, Any], call_ids: list[str], first_number: int
) -> tuple[dict[str, Any], int]:
    calls = []
    number = first_number
    for raw_call in message.get("tool_calls") or []:
        raw = raw_call if isinstance(raw_call, dict) else {}
        number += 1
        call_id = str(raw.get("id") or f"{_TOOL_CALL_ID_PREFIX}{number}")
        call_ids.append(call_id)
        calls.append(_openai_tool_call(raw, call_id))
    converted = {
        "role": "assistant",
        "content": message.get("content") or "",
        "tool_calls": calls,
    }
    return converted, number


def _tool_message(message: dict[str, Any], call_ids: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "tool", "content": message.get("content") or ""}
    if call_ids:
        out["tool_call_id"] = call_ids.pop(0)
    name = message.get("name")
    if name:
        out["name"] = name
    return out


def _message_with_images(message: dict[str, Any]) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    text = message.get("content")
    if text:
        parts.append(_text_part(str(text)))
    parts.extend(_image_part(str(image)) for image in message.get("images") or [])
    return {"role": message.get("role", "user"), "content": parts}


def to_openai_messages(messages: list[dict[str, Any]], system: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    pending_call_ids: list[str] = []
    call_number = 0
    for raw_message in messages:
        message = raw_message if isinstance(raw_message, dict) else {}
        role = message.get("role")
        if message.get("tool_calls"):
            converted, call_number = _assistant_message(message, pending_call_ids, call_number)
            out.append(converted)
        elif role == "tool":
            out.append(_tool_message(message, pending_call_ids))
        elif message.get("images"):
            out.append(_message_with_images(message))
        else:
            out.append(dict(message))
    return out


def think_fields(think: bool | str | None) -> dict[str, Any]:
    if think is None:
        return {}
    if think is False or think == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    budget = _THINK_BUDGET_TOKENS.get(str(think), _THINK_BUDGET_HIGH_TOKENS)
    return {"reasoning_budget_tokens": budget}


def options_fields(options: dict[str, Any] | None) -> dict[str, Any]:
    if not options:
        return {}
    out: dict[str, Any] = {
        name: options[name] for name in _SAMPLING_FIELDS if name in options
    }
    if "num_predict" in options:
        out["max_tokens"] = options["num_predict"]
    return out


def _arguments_from_openai(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _ollama_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return {
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": _arguments_from_openai(function.get("arguments")),
        }
    }


def _openai_message_to_ollama(message: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    reasoning = message.get("reasoning_content")
    if reasoning:
        out["thinking"] = reasoning
    calls = message.get("tool_calls")
    if calls:
        out["tool_calls"] = [_ollama_tool_call(call) for call in calls if isinstance(call, dict)]
    return out


def _first_choice(data: dict[str, Any]) -> dict[str, Any]:
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def from_openai_response(data: dict[str, Any], model: str, elapsed_s: float) -> dict[str, Any]:
    choice = _first_choice(data)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return {
        "model": data.get("model") or model,
        "message": _openai_message_to_ollama(choice.get("message") or {}),
        "done_reason": "stop" if choice.get("finish_reason") != _FINISH_LENGTH else _FINISH_LENGTH,
        "prompt_eval_count": int(usage.get("prompt_tokens") or 0),
        "eval_count": int(usage.get("completion_tokens") or 0),
        "total_duration": int(elapsed_s * 1_000_000_000),
    }


def _read_api_key(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ProtoError(ERR_INTERNAL, f"openai: не удалось прочитать файл ключа {path}") from exc


def _auth_headers(cfg: LlmConfig) -> dict[str, str]:
    if cfg.openai_api_key_file is None:
        return {}
    key = _read_api_key(cfg.openai_api_key_file)
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def _post_json_sync(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        _append_error_body(exc)
        raise


async def _post_with_retry(
    cfg: LlmConfig, url: str, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.request_timeout_s
    last_exc: Exception | None = None
    for attempt in range(1, _POST_RETRY_ATTEMPTS + 1):
        left = deadline - loop.time()
        if left <= 0:
            break
        try:
            return await asyncio.to_thread(_post_json_sync, url, payload, headers, left)
        except JSON_FETCH_ERRORS as exc:
            last_exc = exc
            out_of_attempts = attempt >= _POST_RETRY_ATTEMPTS
            if out_of_attempts or not _is_transient_connection_error(exc):
                break
            if deadline - loop.time() <= _POST_RETRY_DELAY_S:
                break
            log.warning(
                "llm: %s оборвался (%s) — повтор через %.0fс (попытка %d/%d)",
                url,
                exc,
                _POST_RETRY_DELAY_S,
                attempt,
                _POST_RETRY_ATTEMPTS,
            )
            await asyncio.sleep(_POST_RETRY_DELAY_S)
    if last_exc is None:
        raise ProtoError(ERR_INTERNAL, f"{url}: бюджет {cfg.request_timeout_s:.0f}с истёк")
    raise ProtoError(ERR_INTERNAL, f"{url}: {last_exc}")


async def chat(
    cfg: LlmConfig,
    messages: list[dict[str, Any]],
    system: str,
    tools: list[dict[str, Any]] | None = None,
    think: bool | str | None = None,
    response_format: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": cfg.model,
        "messages": to_openai_messages(messages, system),
        "stream": False,
        **think_fields(think),
        **options_fields(options),
    }
    if tools:
        payload["tools"] = tools
    url = f"{cfg.openai_base_url}{CHAT_COMPLETIONS_PATH}"
    loop = asyncio.get_running_loop()
    started = loop.time()
    data = await _post_with_retry(cfg, url, payload, _auth_headers(cfg))
    return from_openai_response(data, cfg.model, loop.time() - started)


async def generate(cfg: LlmConfig, prompt: str, system: str) -> dict[str, Any]:
    return await chat(cfg, [{"role": "user", "content": prompt}], system, think=False)
