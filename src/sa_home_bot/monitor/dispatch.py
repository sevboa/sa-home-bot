"""ProtoEventDispatcher — события здоровья уходят broadcast'ом по протоколу.

В отличие от Telegram-диспетчера, handled=False, пока не подключён ни один
клиент: pending-флаги в БД монитора не двигаются, и событие повторится на
следующем прогоне — бот, поднявшись, ничего не потеряет.
"""

from __future__ import annotations

from dataclasses import asdict

from sa_home_bot.domain.models import Event, SmartChange
from sa_home_bot.jobs.base import DispatchResult
from sa_home_bot.proto.server import ProtoServer


def event_payload(event: Event) -> dict:
    return {
        "component_id": event.component_id,
        "kind": event.kind,
        "label": event.label,
        "temperature_c": event.temperature_c,
        "at": event.at.isoformat(),
    }


def smart_change_payload(change: SmartChange) -> dict:
    return {
        "component_id": change.component_id,
        "label": change.label,
        "health_from": change.health_from,
        "health_to": change.health_to,
        "attr_changes": [asdict(c) for c in change.attr_changes],
        "at": change.at.isoformat(),
    }


class ProtoEventDispatcher:
    def __init__(self, server: ProtoServer) -> None:
        self._server = server

    async def dispatch_alert(self, event: Event) -> DispatchResult:
        return await self._broadcast(event.type, event_payload(event))

    async def dispatch_clear(self, event: Event) -> DispatchResult:
        return await self._broadcast(event.type, event_payload(event))

    async def dispatch_smart(self, change: SmartChange) -> DispatchResult:
        return await self._broadcast(change.event_type, smart_change_payload(change))

    async def _broadcast(self, event_type: str, data: dict) -> DispatchResult:
        delivered = await self._server.broadcast_event(event_type, data)
        return DispatchResult(delivered=delivered > 0, handled=delivered > 0)
