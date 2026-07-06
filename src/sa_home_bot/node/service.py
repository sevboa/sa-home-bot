"""NodeService — управление нодой по протоколу v0 (клиент — nodectl).

`get_state` — статус служб под супервизией; действия start/stop/restart
объявлены в describe с параметром ``name`` и валидируются сервером по нему.
"""

from __future__ import annotations

import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)

SERVICE_NAME = "node"

ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_RESTART = "restart"

_NAME_PARAM = ActionParam(name="name", type="string", required=True, title="Служба")


class NodeService:
    def __init__(self, supervisor: Supervisor) -> None:
        self._supervisor = supervisor
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=("supervisor",),
            actions=(
                ActionSpec(id=ACTION_START, title="Запустить службу", params=(_NAME_PARAM,)),
                ActionSpec(id=ACTION_STOP, title="Остановить службу", params=(_NAME_PARAM,)),
                ActionSpec(
                    id=ACTION_RESTART, title="Перезапустить службу", params=(_NAME_PARAM,)
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "version": __version__,
            "services": [svc.to_dict() for svc in self._supervisor.services.values()],
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", ""))
        svc = self._supervisor.get(name)
        if svc is None:
            known = ", ".join(self._supervisor.services) or "нет служб"
            raise ProtoError(ERR_BAD_REQUEST, f"нет такой службы: {name!r} (есть: {known})")
        if action == ACTION_START:
            await svc.start()
        elif action == ACTION_STOP:
            await svc.stop()
        elif action == ACTION_RESTART:
            await svc.restart()
        else:
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
            raise ValueError(f"необъявленное действие: {action}")
        return {"service": svc.to_dict()}
