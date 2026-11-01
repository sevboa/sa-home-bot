from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.usage import usage_cost

MODEL_TIMEOUT_S = 1800.0
ERROR_SNIPPET_CHARS = 300


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
        return parse_reply(data)


def error_text(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return body[:ERROR_SNIPPET_CHARS]
    message = parsed.get(protocol.FIELD_ERROR) if isinstance(parsed, dict) else None
    return str(message if message is not None else body)[:ERROR_SNIPPET_CHARS]


def parse_reply(data: Any) -> ModelReply:
    try:
        message = data["choices"][0]["message"]
        calls = tuple(
            ToolCall(
                str(call["id"]),
                str(call["function"]["name"]),
                str(call["function"].get("arguments") or "{}"),
            )
            for call in message.get("tool_calls") or ()
        )
        usage = data.get("usage") or {}
        return ModelReply(
            content=str(message.get("content") or ""),
            tool_calls=calls,
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            cost_usd=usage_cost(usage),
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise ModelError(f"ответ модели не разобран: {type(exc).__name__}") from exc


ModelFactory = Callable[[str, str], ChatModel]
