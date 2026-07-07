"""Доменная модель подписки. Без quiet_hours (сознательно, см. ARCHITECTURE §4.5)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

WILDCARD = "*"


@dataclass(frozen=True)
class Subscription:
    name: str
    chat_id: int
    event_types: frozenset[str] = field(default_factory=frozenset)
    allowed_commands: frozenset[str] = field(default_factory=frozenset)
    broken: bool = False

    def accepts_event(self, event_type: str) -> bool:
        if self.broken:
            return False
        return WILDCARD in self.event_types or event_type in self.event_types

    def allows_command(self, command: str) -> bool:
        if self.broken:
            return False
        return command in self.allowed_commands

    def allows_action(self, action_id: str, service: str) -> bool:
        """Право на действие службы (кнопки из describe).

        Полная форма в конфиге — ``действие@служба`` (``restart@node``);
        голое имя (``scan_now``) тоже принимается — совместимость со старыми
        конфигами. С появлением удалённых нод форма расширится до
        ``действие@служба@нода``.
        """
        if self.broken:
            return False
        return (
            f"{action_id}@{service}" in self.allowed_commands
            or action_id in self.allowed_commands
        )

    def with_broken(self, broken: bool = True) -> Subscription:
        return replace(self, broken=broken)
