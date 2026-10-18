from __future__ import annotations

from dataclasses import dataclass, field, replace

WILDCARD = "*"

SOURCE_CONFIG = "config"
SOURCE_GUEST = "guest"


@dataclass(frozen=True)
class Subscription:
    name: str
    chat_id: int
    event_types: frozenset[str] = field(default_factory=frozenset)
    allowed_commands: frozenset[str] = field(default_factory=frozenset)
    broken: bool = False
    source: str = SOURCE_CONFIG
    invited_by_chat_id: int = 0
    invited_at: str = ""
    invited_user: str = ""

    @property
    def is_guest(self) -> bool:
        return self.source == SOURCE_GUEST

    @property
    def is_owner(self) -> bool:
        return self.allows_command(WILDCARD)

    def accepts_event(self, event_type: str) -> bool:
        if self.broken:
            return False
        return WILDCARD in self.event_types or event_type in self.event_types

    def allows_command(self, command: str) -> bool:
        if self.broken:
            return False
        return WILDCARD in self.allowed_commands or command in self.allowed_commands

    def allows_action(self, action_id: str, service: str) -> bool:
        if self.broken:
            return False
        return (
            WILDCARD in self.allowed_commands
            or f"{WILDCARD}@{service}" in self.allowed_commands
            or f"{action_id}@{service}" in self.allowed_commands
            or action_id in self.allowed_commands
        )

    def with_broken(self, broken: bool = True) -> Subscription:
        return replace(self, broken=broken)

    def with_allowed_commands(self, allowed_commands: frozenset[str]) -> Subscription:
        return replace(self, allowed_commands=allowed_commands)
