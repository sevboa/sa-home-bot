from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.agent.model import ChatModel, ModelReply, PaidFreeModelError

PAID_FREE_TEXT = "бесплатная модель начала тратить деньги: ${cost:.4f} за один ответ"


class SpendState(BaseModel):
    base_cost: float = 0.0
    base_tokens: int = 0
    cost: float = 0.0
    tokens: int = 0
    replies: int = 0
    reported_cost: float = 0.0
    reported_tokens: int = 0
    reasoning_tokens: int = 0
    usage_replies: int = 0
    extensions: int = 0
    model: str = ""


class JobSpend:
    def __init__(self, state: SpendState, save: Callable[[SpendState], None]) -> None:
        self._state = state
        self._save = save

    @property
    def extensions(self) -> int:
        return self._state.extensions

    def total_cost(self) -> float:
        return self._state.base_cost + self._state.cost

    def replies(self) -> int:
        return self._state.replies

    def total_tokens(self) -> int:
        return self._state.base_tokens + self._state.tokens

    def _update(self, **changes: Any) -> None:
        self._state = self._state.model_copy(update=changes)
        self._save(self._state)

    def record(self, reply: ModelReply) -> None:
        state = self._state
        self._update(
            replies=state.replies + 1,
            tokens=state.tokens + reply.tokens_in + reply.tokens_out,
            cost=state.cost + (reply.cost_usd or 0.0),
            reasoning_tokens=state.reasoning_tokens + (reply.tokens_reasoning or 0),
            usage_replies=state.usage_replies + (1 if reply.tokens_in + reply.tokens_out else 0),
        )

    def extend(self) -> None:
        self._update(extensions=self._state.extensions + 1)

    def switch_model(self, model: str) -> None:
        if self._state.model and self._state.model != model:
            self._update(model=model, extensions=self._state.extensions + 1)
        elif not self._state.model:
            self._update(model=model)

    def live(self) -> dict[str, Any]:
        state = self._state
        live: dict[str, Any] = {
            protocol.FIELD_MODEL_STEPS: state.replies,
            protocol.PAYLOAD_TOKENS: self.total_tokens(),
            protocol.FIELD_SEGMENT_TOKENS: state.tokens,
            protocol.FIELD_USAGE_KNOWN: state.usage_replies > 0 or state.replies == 0,
        }
        if state.reasoning_tokens:
            live[protocol.FIELD_REASONING_TOKENS] = state.reasoning_tokens
        return live

    def take_report(self) -> dict[str, Any]:
        state = self._state
        report = {
            protocol.PAYLOAD_TOKENS: state.tokens - state.reported_tokens,
            protocol.PAYLOAD_COST_USD: max(0.0, state.cost - state.reported_cost),
        }
        self._update(reported_tokens=state.tokens, reported_cost=state.cost)
        return report


class MeteredChat:
    def __init__(self, inner: ChatModel, spend: JobSpend) -> None:
        self._inner = inner
        self._spend = spend

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        extra: dict[str, Any] | None = None,
    ) -> ModelReply:
        reply = await (
            self._inner.complete(messages, tools, extra=extra)
            if extra
            else self._inner.complete(messages, tools)
        )
        self._spend.record(reply)
        if reply.paid_free:
            raise PaidFreeModelError(PAID_FREE_TEXT.format(cost=reply.cost_usd or 0.0))
        return reply
