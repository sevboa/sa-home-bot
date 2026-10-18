from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sa_home_bot.bot.telegram_retry import REQUEST_TIMEOUT_S, call_with_network_retry
from sa_home_bot.config import GuestSubscriptionConfig, SubscriptionConfig
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationIssue:
    chat_id: int
    name: str
    reason: str


RETIRED_RIGHTS = frozenset({"tell_guests@llm"})


def guest_subscription(cfg: GuestSubscriptionConfig) -> Subscription:
    return Subscription(
        name=cfg.name,
        chat_id=cfg.chat_id,
        event_types=frozenset(cfg.event_types),
        allowed_commands=frozenset(cfg.allowed_commands) - RETIRED_RIGHTS,
        source=SOURCE_GUEST,
        invited_by_chat_id=cfg.invited_by_chat_id,
        invited_at=cfg.invited_at,
        invited_user=cfg.invited_user,
    )


class SubscriptionBook:
    def __init__(self, subscriptions: list[Subscription]) -> None:
        self._subs = list(subscriptions)
        self._by_chat = {s.chat_id: s for s in self._subs}

    @classmethod
    def from_config(
        cls,
        configs: list[SubscriptionConfig],
        guests: Sequence[GuestSubscriptionConfig] = (),
    ) -> SubscriptionBook:
        subs = [
            Subscription(
                name=c.name,
                chat_id=c.chat_id,
                event_types=frozenset(c.event_types),
                allowed_commands=frozenset(c.allowed_commands),
            )
            for c in configs
        ]
        known = {s.chat_id for s in subs}
        for g in guests:
            if g.chat_id in known:
                log.warning(
                    "Гостевая подписка chat_id=%s игнорируется — этот чат есть в конфиге",
                    g.chat_id,
                )
                continue
            subs.append(guest_subscription(g))
        return cls(subs)

    def all(self) -> list[Subscription]:
        return list(self._subs)

    def guests(self) -> list[Subscription]:
        return [s for s in self._subs if s.is_guest]

    def for_chat(self, chat_id: int) -> Subscription | None:
        return self._by_chat.get(chat_id)

    def add(self, sub: Subscription) -> None:
        self.remove(sub.chat_id)
        self._subs.append(sub)
        self._by_chat[sub.chat_id] = sub

    def remove(self, chat_id: int) -> bool:
        if chat_id not in self._by_chat:
            return False
        del self._by_chat[chat_id]
        self._subs = [s for s in self._subs if s.chat_id != chat_id]
        return True

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
        issues: list[ValidationIssue] = []
        for sub in list(self._subs):
            try:
                await call_with_network_retry(
                    lambda sub=sub: bot.get_chat(sub.chat_id, request_timeout=REQUEST_TIMEOUT_S),
                    what=f"get_chat({sub.chat_id})",
                    attempts=2,
                    delay_s=3.0,
                )
            except Exception as exc:
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
