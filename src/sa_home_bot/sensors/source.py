from __future__ import annotations

import asyncio
from datetime import datetime

from sa_home_bot.config import SensorsConfig
from sa_home_bot.domain.host import HostMetricReading
from sa_home_bot.domain.models import DiskSummary, SensorReading, SmartSnapshot
from sa_home_bot.sensors import cpu, disks, gpu, host
from sa_home_bot.utils.clock import utc_now


class SensorSource:
    def __init__(self, config: SensorsConfig) -> None:
        self._config = config

    async def read_cpu(self) -> list[SensorReading]:
        if not self._config.cpu.enabled:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, cpu.read_cpu_sync, utc_now(), self._config.lhm.dll_path
        )

    async def read_gpu(self) -> list[SensorReading]:
        if not self._config.gpu.enabled:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, gpu.read_gpu_sync, utc_now())

    async def read_disks(self) -> list[SensorReading]:
        if not self._config.disks.enabled:
            return []
        loop = asyncio.get_running_loop()
        devices = list(self._config.disks.devices)
        return await loop.run_in_executor(
            None, disks.read_disks_sync, devices, utc_now(), self._config.lhm.dll_path
        )

    async def read_host(
        self, kernel_since: datetime | None = None
    ) -> list[HostMetricReading]:
        if not self._config.host.enabled:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            host.read_host_sync,
            utc_now(),
            self._config.host.sample_window_s,
            kernel_since,
        )

    async def read_all(self) -> list[SensorReading]:
        cpu_readings, gpu_readings, disk_readings = await asyncio.gather(
            self.read_cpu(), self.read_gpu(), self.read_disks()
        )
        return [*cpu_readings, *gpu_readings, *disk_readings]

    async def read_smart_snapshots(self) -> list[SmartSnapshot]:
        if not self._config.disks.enabled:
            return []
        loop = asyncio.get_running_loop()
        devices = list(self._config.disks.devices)
        return await loop.run_in_executor(
            None, disks.read_smart_snapshots_sync, devices, utc_now()
        )

    async def read_disk_summaries(
        self, health_overrides: dict[str, str | None]
    ) -> list[DiskSummary]:
        loop = asyncio.get_running_loop()
        devices = list(self._config.disks.devices)
        return await loop.run_in_executor(
            None,
            disks.read_disk_summaries_sync,
            devices,
            health_overrides,
            self._config.lhm.dll_path,
        )
