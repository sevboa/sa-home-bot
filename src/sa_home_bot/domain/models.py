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
EVENT_SMART_DEGRADED = "smart_degraded"  # SMART-здоровье диска ухудшилось
EVENT_SMART_RECOVERED = "smart_recovered"  # SMART-показатели улучшились
EVENT_SYSTEM = "system"

# --- Характер отключения машины (см. sensors/power.py) ---
POWER_CLEAN = "clean"  # штатное выключение/перезагрузка (был shutdown)
POWER_UNEXPECTED = "unexpected"  # внезапное (crash): питание/зависание/reset

# --- SMART-здоровье диска (для сводки /status) ---
DISK_OK = "ok"  # SMART PASSED, нет битых секторов
DISK_WARN = "warning"  # PASSED, но есть pending/uncorrectable сектора
DISK_FAIL = "failed"  # SMART FAILED — диск при смерти


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
class DiskSummary:
    """Краткая сводка по физическому диску для /status.

    Собирается на лету (не из БД): SMART-здоровье и температура — из smartctl
    (только для дисков с известным типом адаптера), свободное место — из точек
    монтирования. `health`/`temperature_c` = None, если SMART недоступен (eMMC).
    """

    label: str  # короткая метка: HDD1, HDD2, eMMC
    health: str | None  # DISK_OK | DISK_WARN | DISK_FAIL | None (недоступно)
    temperature_c: float | None
    free_bytes: int | None
    total_bytes: int | None
    model: str | None = None


@dataclass(frozen=True)
class Event:
    """Событие здоровья, производное от перехода. Рассылается подписчикам."""

    type: str  # EVENT_OVERHEAT_STARTED | EVENT_OVERHEAT_CLEARED
    component_id: str
    kind: str
    label: str
    temperature_c: float
    at: datetime


@dataclass(frozen=True)
class SmartSnapshot:
    """Снимок ключевых SMART-счётчиков одного диска — baseline для дельты.

    Собирается нечастым SmartScanJob из ``smartctl -H -A`` (read-only, без
    self-test'ов). ``attrs`` — сырые (raw) значения только отслеживаемых
    атрибутов (``domain.smart.MONITORED_SMART_ATTRS``), присутствующих в выводе.
    ``health`` — агрегат ``smart_status`` + сбойные сектора (DISK_OK/WARN/FAIL).
    """

    component_id: str  # "disk:/dev/sda" (по realpath устройства)
    label: str  # модель диска
    health: str | None  # DISK_OK | DISK_WARN | DISK_FAIL | None (недоступно)
    attrs: dict[int, int]  # id SMART-атрибута -> raw-значение
    taken_at: datetime


@dataclass(frozen=True)
class SmartAttrChange:
    """Изменение одного SMART-счётчика между снимками."""

    attr_id: int
    name: str
    old: int
    new: int


@dataclass(frozen=True)
class SmartChange:
    """Изменение SMART-состояния диска между снимками (для рассылки).

    ``event_type`` — EVENT_SMART_DEGRADED (рост сбойных секторов / падение
    класса здоровья доминирует) либо EVENT_SMART_RECOVERED (только улучшения).
    """

    component_id: str
    label: str
    event_type: str
    health_from: str | None
    health_to: str | None
    attr_changes: tuple[SmartAttrChange, ...]
    at: datetime
