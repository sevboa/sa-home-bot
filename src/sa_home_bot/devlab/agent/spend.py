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
    reported_cost: float = 0.0
    reported_tokens: int = 0
    extensions: int = 0


class JobSpend:
    def __init__(self, state: SpendState, save: Callable[[SpendState], None]) -> None:
        self._state = state
        self._save = save

    @property
    def extensions(self) -> int:
        return self._state.extensions

    def total_cost(self) -> float:
        return self._state.base_cost + self._state.cost

    def total_tokens(self) -> int:
        return self._state.base_tokens + self._state.tokens

    def _update(self, **changes: Any) -> None:
        self._state = self._state.model_copy(update=changes)
        self._save(self._state)

    def record(self, reply: ModelReply) -> None:
        state = self._state
        self._update(
            tokens=state.tokens + reply.tokens_in + reply.tokens_out,
            cost=state.cost + (reply.cost_usd or 0.0),
        )

    def extend(self) -> None:
        self._update(extensions=self._state.extensions + 1)

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
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        reply = await self._inner.complete(messages, tools)
        self._spend.record(reply)
        if reply.paid_free:
            raise PaidFreeModelError(PAID_FREE_TEXT.format(cost=reply.cost_usd or 0.0))
        return reply
