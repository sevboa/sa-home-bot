from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

OK = "ok"
ALERTING = "alerting"

KIND_CPU = "cpu"
KIND_GPU = "gpu"
KIND_DISK = "disk"

EVENT_OVERHEAT_STARTED = "overheat_started"
EVENT_OVERHEAT_CLEARED = "overheat_cleared"
EVENT_SMART_DEGRADED = "smart_degraded"
EVENT_SMART_RECOVERED = "smart_recovered"
EVENT_SYSTEM = "system"

POWER_CLEAN = "clean"
POWER_UNEXPECTED = "unexpected"

DISK_OK = "ok"
DISK_WARN = "warning"
DISK_FAIL = "failed"


@dataclass(frozen=True)
class SensorReading:
    component_id: str
    kind: str
    label: str
    temperature_c: float
    taken_at: datetime


@dataclass(frozen=True)
class KnownState:
    component_id: str
    status: str
    consecutive_count: int
    alerting_since: datetime | None


@dataclass(frozen=True)
class HealthState:
    component_id: str
    kind: str
    label: str
    status: str
    temperature_c: float
    consecutive_count: int
    alerting_since: datetime | None


@dataclass(frozen=True)
class Transition:
    component_id: str
    kind: str
    label: str
    from_status: str
    to_status: str
    temperature_c: float
    at: datetime


@dataclass(frozen=True)
class HealthDiff:
    states: list[HealthState]
    transitions: list[Transition]


@dataclass(frozen=True)
class PowerEvent:
    kind: str
    boot_at: datetime
    down_at: datetime | None
    up_at: datetime | None
    down_approx: bool = False

    @property
    def downtime(self) -> timedelta | None:
        if self.down_at is None or self.up_at is None:
            return None
        return self.up_at - self.down_at


@dataclass(frozen=True)
class DiskSummary:
    label: str
    health: str | None
    temperature_c: float | None
    free_bytes: int | None
    total_bytes: int | None
    model: str | None = None
    kind: str = "hdd"


@dataclass(frozen=True)
class Event:
    type: str
    component_id: str
    kind: str
    label: str
    temperature_c: float
    at: datetime


@dataclass(frozen=True)
class SmartSnapshot:
    component_id: str
    label: str
    health: str | None
    attrs: dict[int, int]
    taken_at: datetime


@dataclass(frozen=True)
class SmartAttrChange:
    attr_id: int
    name: str
    old: int
    new: int


@dataclass(frozen=True)
class SmartChange:
    component_id: str
    label: str
    event_type: str
    health_from: str | None
    health_to: str | None
    attr_changes: tuple[SmartAttrChange, ...]
    at: datetime
