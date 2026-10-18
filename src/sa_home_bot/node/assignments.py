from __future__ import annotations

from dataclasses import dataclass

from sa_home_bot.node.instances import slot_key

ROLE_ACTIVE = "active"
ROLE_STANDBY = "standby"
ROLES = (ROLE_ACTIVE, ROLE_STANDBY)

_PRIO_PREFIX = "prio="


class AssignmentError(ValueError):
    pass


@dataclass(frozen=True)
class Assignment:
    service: str
    instance: str = ""
    role: str = ROLE_ACTIVE
    priority: int | None = None

    @property
    def key(self) -> str:
        return slot_key(self.service, self.instance)

    @property
    def standby(self) -> bool:
        return self.role == ROLE_STANDBY


def parse(text: str) -> Assignment:
    raw = text.strip()
    if not raw:
        raise AssignmentError("пустое назначение")
    head, *modifiers = raw.split(":")
    service, _, instance = head.partition("@")
    if not service:
        raise AssignmentError(f"нет имени службы: {text!r}")
    role = ROLE_ACTIVE
    priority: int | None = None
    for mod in modifiers:
        mod = mod.strip()
        if mod in ROLES:
            role = mod
        elif mod.startswith(_PRIO_PREFIX):
            try:
                priority = int(mod[len(_PRIO_PREFIX) :])
            except ValueError as exc:
                raise AssignmentError(f"приоритет не число: {mod!r}") from exc
        else:
            raise AssignmentError(f"непонятная часть назначения: {mod!r} в {text!r}")
    return Assignment(service=service, instance=instance, role=role, priority=priority)


def parse_valid(items: list[str]) -> list[Assignment]:
    parsed = []
    for item in items:
        try:
            parsed.append(parse(item))
        except AssignmentError:
            continue
    return parsed


def has_service(items: list[str], service: str) -> bool:
    return any(a.service == service for a in parse_valid(items))


def key_or_raw(text: str) -> str:
    try:
        return parse(text).key
    except AssignmentError:
        return text
