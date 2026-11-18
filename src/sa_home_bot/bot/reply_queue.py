from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sa_home_bot.bot.away import pending_entry
from sa_home_bot.db.store import Store
from sa_home_bot.utils.clock import utc_now

if TYPE_CHECKING:
    from aiogram.types import Message

REPLY_QUEUE_KEY = "ai_reply_queue"
FIELD_ATTEMPTS = "attempts"
FIELD_BLOCKED = "blocked"


class ReplyQueue:
    def __init__(self, store: Store, clock: Callable[[], datetime] = utc_now) -> None:
        self.store = store
        self._clock = clock

    async def _update(
        self,
        mutate: Callable[[dict[str, dict[str, Any]] | None], dict[str, dict[str, Any]] | None],
    ) -> None:
        def fn(raw: str | None) -> str | None:
            new = mutate(json.loads(raw) if raw else None)
            return None if new is None else json.dumps(new, ensure_ascii=False)

        await self.store.update_state(REPLY_QUEUE_KEY, fn)

    async def entries(self) -> dict[str, dict[str, Any]]:
        raw = await self.store.get_state(REPLY_QUEUE_KEY)
        return json.loads(raw) if raw else {}

    async def put(self, message: Message, dialogue_id: int) -> None:
        key = str(message.chat.id)
        entry = pending_entry(
            message, dialogue_id, (message.text or message.caption or "").strip(), self._clock()
        )

        def mutate(current: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
            rest = dict(current or {})
            old = rest.get(key)
            if old is not None:
                fresh = dict(entry)
                fresh["dialogue_id"] = old["dialogue_id"]
                fresh["since"] = old.get("since", entry["since"])
                fresh[FIELD_ATTEMPTS] = old.get(FIELD_ATTEMPTS, 0)
                fresh[FIELD_BLOCKED] = old.get(FIELD_BLOCKED, False)
                rest[key] = fresh
            else:
                rest[key] = {**entry, FIELD_ATTEMPTS: 0, FIELD_BLOCKED: False}
            return rest

        await self._update(mutate)

    async def finish(self, chat_id: int, handled_message_id: int) -> bool:
        key = str(chat_id)
        removed = False

        def mutate(current: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
            nonlocal removed
            if current is None:
                return None
            entry = current.get(key)
            if entry is None or entry["last_message_id"] != handled_message_id:
                return current
            removed = True
            rest = {k: v for k, v in current.items() if k != key}
            return rest or None

        await self._update(mutate)
        return removed

    async def fail(self, chat_id: int) -> int:
        key = str(chat_id)
        updated = 0

        def mutate(current: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
            nonlocal updated
            if current is None:
                return None
            entry = current.get(key)
            if entry is None:
                return current
            updated = entry.get(FIELD_ATTEMPTS, 0) + 1
            entry[FIELD_ATTEMPTS] = updated
            return current

        await self._update(mutate)
        return updated

    async def block(self, chat_id: int) -> bool:
        key = str(chat_id)
        first = False

        def mutate(current: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]] | None:
            nonlocal first
            if current is None:
                return None
            entry = current.get(key)
            if entry is None:
                return current
            first = not entry.get(FIELD_BLOCKED, False)
            entry[FIELD_BLOCKED] = True
            return current

        await self._update(mutate)
        return first
