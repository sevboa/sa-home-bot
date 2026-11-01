from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sa_home_bot.devlab import protocol
from sa_home_bot.utils.process import ProcResult, run

log = logging.getLogger(__name__)

Runner = Callable[[list[str]], Awaitable[ProcResult]]


class SliceControl:
    def __init__(self, runner: Runner = run) -> None:
        self._runner = runner
        self.night = False

    @property
    def weight(self) -> int:
        if self.night:
            return protocol.DEVLAB_SLICE_CPU_WEIGHT_NIGHT
        return protocol.DEVLAB_SLICE_CPU_WEIGHT

    def threads(self, day: int, night: int) -> int:
        return night or day if self.night else day

    def cpus(self, day: str, night: str) -> str:
        return night if self.night else day

    async def apply_weight(self) -> None:
        await self._systemctl(
            "set-property", "--runtime", protocol.DEVLAB_SLICE, f"CPUWeight={self.weight}"
        )

    async def set_night(self, night: bool) -> None:
        self.night = night
        await self.apply_weight()

    async def freeze(self) -> None:
        await self._systemctl("freeze", protocol.DEVLAB_SLICE)

    async def thaw(self) -> None:
        await self._systemctl("thaw", protocol.DEVLAB_SLICE)

    async def _systemctl(self, *args: str) -> None:
        result = await self._runner(["systemctl", "--user", *args])
        if result.returncode != 0:
            log.warning("systemctl --user %s: %s", " ".join(args), result.stderr_text)
