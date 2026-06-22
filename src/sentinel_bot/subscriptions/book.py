"""SubscriptionBook — реестр подписок из конфига (иммутабелен во время работы)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sentinel_bot.config import SubscriptionConfig
from sentinel_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationIssue:
    chat_id: int
    name: str
    reason: str


class SubscriptionBook:
    def __init__(self, subscriptions: list[Subscription]) -> None:
        self._subs = list(subscriptions)
        self._by_chat = {s.chat_id: s for s in self._subs}

    @classmethod
    def from_config(cls, configs: list[SubscriptionConfig]) -> SubscriptionBook:
        subs = [
            Subscription(
                name=c.name,
                chat_id=c.chat_id,
                event_types=frozenset(c.event_types),
                allowed_commands=frozenset(c.allowed_commands),
            )
            for c in configs
        ]
        return cls(subs)

    def all(self) -> list[Subscription]:
        return list(self._subs)

    def for_chat(self, chat_id: int) -> Subscription | None:
        return self._by_chat.get(chat_id)

    def accepting(self, event_type: str) -> list[Subscription]:
        return [s for s in self._subs if s.accepts_event(event_type)]

    def _mark_broken(self, chat_id: int) -> None:
        sub = self._by_chat.get(chat_id)
        if sub is None:
            return
        broken = sub.with_broken(True)
        self._by_chat[chat_id] = broken
        self._subs = [broken if s.chat_id == chat_id else s for s in self._subs]

    async def validate_on_startup(self, bot) -> list[ValidationIssue]:
        """Проверить доступность чатов через bot.get_chat; недоступные → broken."""
        issues: list[ValidationIssue] = []
        for sub in list(self._subs):
            try:
                await bot.get_chat(sub.chat_id)
            except Exception as exc:  # noqa: BLE001
                self._mark_broken(sub.chat_id)
                issue = ValidationIssue(sub.chat_id, sub.name, str(exc))
                issues.append(issue)
                log.warning(
                    "Подписка '%s' (chat_id=%s) недоступна: %s — помечена broken",
                    sub.name,
                    sub.chat_id,
                    exc,
                )
        return issues
