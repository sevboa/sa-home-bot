from __future__ import annotations

from typing import Any

from sa_home_bot import __version__
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.executor.busy import BusyTracker
from sa_home_bot.devlab.executor.park import ParkMode
from sa_home_bot.proto.messages import (
    ERR_UNKNOWN_ACTION,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)


class ExecutorService:
    def __init__(self, node: str, busy: BusyTracker, park: ParkMode | None = None) -> None:
        self._node = node
        self._busy = busy
        self._park = park

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(
                node=self._node,
                service=protocol.EXECUTOR_SERVICE,
                version=__version__,
                busy=self._busy.busy(),
                working=self._busy.working(),
            ),
            actions=(ActionSpec(id=protocol.ACTION_PARK, title="Запарковать исполнитель"),),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            protocol.FIELD_BUSY: self._busy.busy(),
            protocol.FIELD_JOBS: self._busy.running_jobs(),
            protocol.FIELD_WAITING: self._busy.holding_for_owner(),
            protocol.FIELD_OUTBOX: self._busy.outbox_size(),
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == protocol.ACTION_PARK:
            if self._park is not None:
                self._park.active = True
            self._busy.set_parked(True)
            return {protocol.FIELD_PARKED: True}
        raise ProtoError(ERR_UNKNOWN_ACTION, f"нет такого действия: {action}")
