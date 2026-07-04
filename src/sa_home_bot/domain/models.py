"""Доменные типы. Чистые dataclass'ы без зависимостей от инфраструктуры.

Сущность домена — показание датчика (`SensorReading`) и производное состояние
здоровья компонента (`HealthState`). Уведомление — функция от перехода состояния
(`Transition`), а не от мгновенного значения.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# --- Статусы компонента ---
OK = "ok"
ALERTING = "alerting"

# --- Виды компонентов ---
KIND_CPU = "cpu"
KIND_DISK = "disk"

# --- Типы событий ---
EVENT_OVERHEAT_STARTED = "overheat_started"
EVENT_OVERHEAT_CLEARED = "overheat_cleared"
EVENT_SYSTEM = "system"

# --- Характер отключения машины (см. sensors/power.py) ---
POWER_CLEAN = "clean"  # штатное выключение/перезагрузка (был shutdown)
POWER_UNEXPECTED = "unexpected"  # внезапное (crash): питание/зависание/reset


@dataclass(frozen=True)
class SensorReading:
    """Мгновенное показание одного компонента."""

    component_id: str  # "cpu:package" / "disk:/dev/sda"
    kind: str  # KIND_CPU | KIND_DISK
    label: str  # человекочитаемое имя
    temperature_c: float
    taken_at: datetime


@dataclass(frozen=True)
class KnownState:
    """Состояние компонента, известное из БД (вход reconciliation)."""

    component_id: str
    status: str  # OK | ALERTING
    consecutive_count: int  # счётчик для гистерезиса
    alerting_since: datetime | None


@dataclass(frozen=True)
class HealthState:
    """Новое вычисленное состояние компонента (выход reconciliation, в БД)."""

    component_id: str
    kind: str
    label: str
    status: str  # OK | ALERTING
    temperature_c: float
    consecutive_count: int
    alerting_since: datetime | None


@dataclass(frozen=True)
class Transition:
    """Переход состояния компонента между двумя срезами."""

    component_id: str
    kind: str
    label: str
    from_status: str
    to_status: str
    temperature_c: float
    at: datetime


@dataclass(frozen=True)
class HealthDiff:
    """Результат reconciliation: новые состояния всех компонентов + переходы."""

    states: list[HealthState]
    transitions: list[Transition]


@dataclass(frozen=True)
class PowerEvent:
    """Одно отключение машины, восстановленное из журнала загрузок (`last`).

    Каждая завершённая boot-сессия даёт одно событие: либо штатное выключение
    (`POWER_CLEAN` — `down_at` = точный момент shutdown), либо внезапный обрыв
    (`POWER_UNEXPECTED` — в wtmp момента выключения нет, только факт `crash`;
    `down_at` оценивается по последнему событию systemd-журнала перед обрывом,
    поле `down_approx=True`). `up_at` — когда машина поднялась снова.
    """

    kind: str  # POWER_CLEAN | POWER_UNEXPECTED
    boot_at: datetime  # старт сессии, которая так завершилась (для сопоставления с journal)
    down_at: datetime | None  # когда машина погасла (для unexpected может быть None)
    up_at: datetime | None  # когда поднялась снова
    down_approx: bool = False  # down_at приблизителен (из journal, а не точный shutdown)

    @property
    def downtime(self) -> timedelta | None:
        """Длительность простоя, если известны оба конца."""
        if self.down_at is None or self.up_at is None:
            return None
        return self.up_at - self.down_at


@dataclass(frozen=True)
class Event:
    """Событие здоровья, производное от перехода. Рассылается подписчикам."""

    type: str  # EVENT_OVERHEAT_STARTED | EVENT_OVERHEAT_CLEARED
    component_id: str
    kind: str
    label: str
    temperature_c: float
    at: datetime
