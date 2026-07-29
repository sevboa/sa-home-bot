"""SubscriptionBook — реестр подписок: владельческие из конфига + гостевые.

Владельческие подписки по-прежнему иммутабельны: правка = правка конфига и
рестарт. Гостевые (выданные инвайт-кодом, AUTHORIZATION.md §10) добавляются и
снимаются в рантайме — иначе приглашённому пришлось бы ждать перезапуска бота
ровно в тот момент, когда его только что впустили. Персистентность у них
отдельная — гостевой пакет (`subscriptions/guests.py`), книга её не касается.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from sa_home_bot.config import GuestSubscriptionConfig, SubscriptionConfig
from sa_home_bot.subscriptions.models import SOURCE_GUEST, Subscription

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationIssue:
    chat_id: int
    name: str
    reason: str


def guest_subscription(cfg: GuestSubscriptionConfig) -> Subscription:
    """Гостевая запись пакета → подписка (одно место сборки на все пути)."""
    return Subscription(
        name=cfg.name,
        chat_id=cfg.chat_id,
        event_types=frozenset(cfg.event_types),
        allowed_commands=frozenset(cfg.allowed_commands),
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
            # Владельческая подписка старше гостевой: если админ вписал этот
            # чат руками, гостевая запись — след прошлого приглашения, и
            # молча урезать человеку права она не должна.
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
        """Добавить подписку в рантайме (гость по инвайту).

        Повторная выдача на тот же чат заменяет запись — так активация кода
        идемпотентна: гость, приславший второй код, не получает дубль.
        """
        self.remove(sub.chat_id)
        self._subs.append(sub)
        self._by_chat[sub.chat_id] = sub

    def remove(self, chat_id: int) -> bool:
        """Убрать подписку (отзыв гостя). False — такой не было."""
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
