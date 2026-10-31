from __future__ import annotations

import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.proto.server import undeclared_action


class HubService:
    def __init__(self, store: HubStore) -> None:
        self._store = store
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=protocol.SERVICE_NAME, version=__version__),
            capabilities=(protocol.ACTION_ISSUE_TOKEN, protocol.ACTION_REVOKE_TOKEN),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": protocol.SERVICE_NAME,
            "active_tokens": await self._store.count_active(),
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._dispatch(action, args)
        except HubError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc

    async def _dispatch(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", ""))
        if action == protocol.ACTION_ISSUE_TOKEN:
            role = str(args.get("role", ""))
            return {"name": name, "role": role, "token": await self._store.issue_token(name, role)}
        if action == protocol.ACTION_REVOKE_TOKEN:
            await self._store.revoke_token(name)
            return {"name": name, "revoked": True}
        raise undeclared_action(action)
