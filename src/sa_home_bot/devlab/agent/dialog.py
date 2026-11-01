from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

ANSWER_PREFIX = "ответ владельца: "


class Dialog(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    question: str = ""
    call_id: str | None = None
    answer: str | None = None

    def resumed_messages(self) -> list[dict[str, Any]]:
        if self.answer is None:
            raise ValueError("диалог без ответа владельца не продолжается")
        reply = ANSWER_PREFIX + self.answer
        if self.call_id is None:
            return [*self.messages, {"role": "user", "content": reply}]
        return [*self.messages, {"role": "tool", "tool_call_id": self.call_id, "content": reply}]
