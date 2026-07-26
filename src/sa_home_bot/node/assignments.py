"""Назначение службы ноде: что именно, в какой роли и с каким приоритетом.

Раньше назначение было просто именем службы (``"monitor"``). С появлением
резервных служб этого мало: нужно различать «держи эту службу запущенной» и
«держи её наготове, но не запускай, пока её держит другой», а у службы с
несколькими инстансами (разные боты) — ещё и о каком из них речь.

Формат строки (он же лежит в ``[node].assignments`` и в ``node-state.json``)::

    <служба>[@<инстанс>][:<роль>][:prio=<число>]

    monitor                          — как раньше: активная, без инстансов
    telegram-bot@alfred              — активный экземпляр бота «alfred»
    telegram-bot@alfred:standby      — тот же бот в резерве
    telegram-bot@alfred:standby:prio=70

Старые плоские строки читаются без изменений и означают ровно то же, что и
означали, — существующие конфиги и состояния нод не требуют миграции.
"""

from __future__ import annotations

from dataclasses import dataclass

from sa_home_bot.node.instances import slot_key

ROLE_ACTIVE = "active"
ROLE_STANDBY = "standby"
ROLES = (ROLE_ACTIVE, ROLE_STANDBY)

_PRIO_PREFIX = "prio="


class AssignmentError(ValueError):
    """Строка назначения не разбирается."""


@dataclass(frozen=True)
class Assignment:
    """Разобранное назначение.

    ``priority`` — ``None`` означает «взять из типа машины» (`node/kind.py`);
    явное число нужно там, где владелец хочет иной порядок, чем подсказывает
    железо.
    """

    service: str
    instance: str = ""
    role: str = ROLE_ACTIVE
    priority: int | None = None

    @property
    def key(self) -> str:
        """Ключ слота: по нему живёт запись в супервизоре и имя пакета."""
        return slot_key(self.service, self.instance)

    @property
    def standby(self) -> bool:
        return self.role == ROLE_STANDBY

    def to_str(self) -> str:
        text = self.key
        if self.role != ROLE_ACTIVE:
            text += f":{self.role}"
        if self.priority is not None:
            text += f":{_PRIO_PREFIX}{self.priority}"
        return text


def parse(text: str) -> Assignment:
    """Разобрать строку назначения. Пустая/битая — AssignmentError."""
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


def has_service(items: list[str], service: str) -> bool:
    """Назначена ли ноде служба — в любой роли и с любым инстансом."""
    for item in items:
        try:
            if parse(item).service == service:
                return True
        except AssignmentError:
            continue
    return False


def parse_all(items: list[str]) -> list[Assignment]:
    """Разобрать список назначений, пропуская непонятные (с исключением наверх
    разбирается вызывающий — здесь только форма строки)."""
    return [parse(item) for item in items]
