from __future__ import annotations

from typing import Any

from sa_home_bot import __version__
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.proto.messages import (
    ERR_UNKNOWN_ACTION,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)


class ExecutorService:
    def __init__(self, node: str, busy: BusyTracker) -> None:
        self._node = node
        self._busy = busy

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(
                node=self._node,
                service=protocol.EXECUTOR_SERVICE,
                version=__version__,
                busy=self._busy.busy(),
            )
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            protocol.FIELD_BUSY: self._busy.busy(),
            protocol.FIELD_JOBS: self._busy.running_jobs(),
            protocol.FIELD_WAITING: self._busy.holding_for_owner(),
            protocol.FIELD_OUTBOX: self._busy.outbox_size(),
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        raise ProtoError(ERR_UNKNOWN_ACTION, f"нет такого действия: {action}")
