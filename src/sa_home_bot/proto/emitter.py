from __future__ import annotations

from typing import Any

from sa_home_bot.proto.server import ProtoServer


class DeferredEmitter:
    def __init__(self) -> None:
        self._server: ProtoServer | None = None

    def bind(self, server: ProtoServer) -> None:
        self._server = server

    async def __call__(self, event_type: str, data: dict[str, Any]) -> None:
        if self._server is not None:
            await self._server.broadcast_event(event_type, data)
