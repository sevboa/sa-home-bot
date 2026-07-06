"""Контракт job'ов: протокол SensorJob, контекст, результат и шов доставки.

`EventDispatcher` — куда job отдаёт события здоровья. В процессе бота это
рассылка по Telegram-чатам (`bot.dispatch.TelegramEventDispatcher`), в процессе
монитора — broadcast по протоколу (`monitor.dispatch.ProtoEventDispatcher`).
Сам job не знает, кто его слушает.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sa_home_bot.config import Settings
    from sa_home_bot.db.store import Store
    from sa_home_bot.domain.models import Event, SmartChange
    from sa_home_bot.sensors.source import SensorSource


@dataclass(frozen=True)
class DispatchResult:
    """Итог доставки события.

    `handled` — событие принято, можно двигать флаг notified/baseline и не
    повторять. `delivered` — реально дошло хоть до одного адресата (метрика).
    Telegram-диспетчер всегда handled=True (повтор дал бы дубли живым чатам);
    proto-диспетчер handled=False, когда клиентов нет, — событие повторится.
    """

    delivered: bool
    handled: bool


class EventDispatcher(Protocol):
    async def dispatch_alert(self, event: Event) -> DispatchResult: ...

    async def dispatch_clear(self, event: Event) -> DispatchResult: ...

    async def dispatch_smart(self, change: SmartChange) -> DispatchResult: ...


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
