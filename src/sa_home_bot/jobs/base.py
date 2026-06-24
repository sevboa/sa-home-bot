"""Контракт job'ов: протокол SensorJob, контекст и результат."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sa_home_bot.config import Settings
    from sa_home_bot.db.store import Store
    from sa_home_bot.sensors.source import SensorSource
    from sa_home_bot.subscriptions.book import SubscriptionBook


class NotifierProtocol(Protocol):
    async def send_direct(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> int | None:
        """Отправить сообщение. Вернуть message_id при успехе, иначе None."""
        ...


@dataclass
class JobContext:
    store: Store
    sensors: SensorSource
    notifier: NotifierProtocol
    subscriptions: SubscriptionBook
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
