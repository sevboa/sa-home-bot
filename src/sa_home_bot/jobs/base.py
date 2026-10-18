from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sa_home_bot.config import Settings
    from sa_home_bot.db.store import Store
    from sa_home_bot.domain.host import HostEvent
    from sa_home_bot.domain.models import Event, SmartChange
    from sa_home_bot.sensors.source import SensorSource


@dataclass(frozen=True)
class DispatchResult:
    delivered: bool
    handled: bool


class EventDispatcher(Protocol):
    async def dispatch_alert(self, event: Event) -> DispatchResult: ...

    async def dispatch_clear(self, event: Event) -> DispatchResult: ...

    async def dispatch_smart(self, change: SmartChange) -> DispatchResult: ...

    async def dispatch_host_alert(self, event: HostEvent) -> DispatchResult: ...

    async def dispatch_host_clear(self, event: HostEvent) -> DispatchResult: ...


@dataclass
class JobContext:
    store: Store
    sensors: SensorSource
    dispatcher: EventDispatcher
    config: Settings


@dataclass
class JobResult:
    components_scanned: int = 0
    transitions: int = 0
    alerts_sent: int = 0
    clears_sent: int = 0
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class SensorJob(Protocol):
    @property
    def dedup_key(self) -> str: ...

    @property
    def job_type(self) -> str: ...

    async def run(self, ctx: JobContext) -> JobResult: ...
