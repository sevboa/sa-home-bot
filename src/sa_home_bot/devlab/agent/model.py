from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.usage import usage_cost

MODEL_TIMEOUT_S = 1800.0
ERROR_SNIPPET_CHARS = 300
RAW_LOG_CHARS = 1500
FINISH_ERROR = "error"

log = logging.getLogger(__name__)


class ModelError(Exception):
    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class PaidFreeModelError(ModelError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    paid_free: bool = False

    def as_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message


class ChatModel(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


class GatewayChat:
    def __init__(
        self,
        base_url: str,
        token: str,
        model: str,
        job_id: str,
        timeout_s: float = MODEL_TIMEOUT_S,
        temperature: float = 0.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + protocol.HTTP_CHAT_PATH
        self._headers = {"Authorization": f"Bearer {token}", protocol.HEADER_JOB: job_id}
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._temperature = temperature
        self._owns_session = session is None
        self._session = session or aiohttp.ClientSession()

    async def close(self) -> None:
        if self._owns_session:
            await self._session.close()

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        body = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": self._temperature,
        }
        try:
            async with self._session.post(
                self._url, json=body, headers=self._headers, timeout=self._timeout
            ) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise ModelError(
                        f"шлюз ответил HTTP {resp.status}: {error_text(text)}",
                        transient=resp.status >= 500 or resp.status == 429,
                    )
                data = await resp.json(content_type=None)
        except TimeoutError as exc:
            raise ModelError(f"модель не ответила за {self._timeout.total:g} с") from exc
        except aiohttp.ClientError as exc:
            raise ModelError(f"шлюз недоступен: {type(exc).__name__}", transient=True) from exc
        return parse_reply(data, len(messages))


def error_text(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:ERROR_SNIPPET_CHARS]
    message = parsed.get(protocol.FIELD_ERROR) if isinstance(parsed, dict) else None
    return str(message if message is not None else body)[:ERROR_SNIPPET_CHARS]


def provider_error(data: Any) -> ModelError | None:
    if not isinstance(data, dict):
        return None
    error = data.get(protocol.FIELD_ERROR)
    choices = data.get("choices")
    if error is None and isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict) and first.get("finish_reason") == FINISH_ERROR:
            error = first.get(protocol.FIELD_ERROR) or "finish_reason=error"
    if error is None:
        return None
    if isinstance(error, dict):
        text = str(error.get("message") or error)
        code: Any = error.get("code")
    else:
        text, code = str(error), None
    try:
        transient = int(code) == 429 or int(code) >= 500
    except (TypeError, ValueError):
        transient = True
    return ModelError(f"провайдер вернул ошибку: {text[:ERROR_SNIPPET_CHARS]}", transient=transient)


def raw_snippet(data: Any) -> str:
    try:
        text = json.dumps(data, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(data)
    return text[:RAW_LOG_CHARS]


def parse_calls(message: dict[str, Any], step: int) -> tuple[ToolCall, ...]:
    calls = []
    for index, call in enumerate(message.get("tool_calls") or ()):
        function = call["function"]
        name = function.get("name")
        if not name:
            raise ModelError("ответ модели не разобран: вызов инструмента без имени")
        calls.append(
            ToolCall(
                str(call.get("id") or f"call_{step}_{index}"),
                str(name),
                str(function.get("arguments") or "{}"),
            )
        )
    return tuple(calls)


def parse_reply(data: Any, step: int = 0) -> ModelReply:
    failure = provider_error(data)
    if failure is None:
        try:
            message = data["choices"][0]["message"]
            calls = parse_calls(message, step)
            usage = data.get("usage") or {}
            return ModelReply(
                content=str(message.get("content") or ""),
                tool_calls=calls,
                tokens_in=int(usage.get("prompt_tokens") or 0),
                tokens_out=int(usage.get("completion_tokens") or 0),
                cost_usd=usage_cost(usage),
            )
        except ModelError as exc:
            failure = exc
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            empty = isinstance(data, dict) and not data.get("choices")
            failure = ModelError(
                f"ответ модели не разобран: {type(exc).__name__}"
                + (" (нет choices)" if empty else ""),
                transient=empty,
            )
    log.warning("Ответ модели не разобран (%s): %s", failure, raw_snippet(data))
    raise failure


ModelFactory = Callable[[str, str], ChatModel]
