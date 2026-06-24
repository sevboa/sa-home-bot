"""SensorSource — изоляция доменного типа SensorReading от источников.

Все блокирующие вызовы (psutil, smartctl-подпроцесс) уходят в executor, чтобы
не блокировать event loop (инвариант ARCHITECTURE.md §9.6).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sa_home_bot.config import SensorsConfig
from sa_home_bot.domain.models import SensorReading
from sa_home_bot.sensors import cpu, disks


def _now() -> datetime:
    return datetime.now(tz=UTC)


class SensorSource:
    def __init__(self, config: SensorsConfig) -> None:
        self._config = config

    async def read_cpu(self) -> list[SensorReading]:
        if not self._config.cpu.enabled:
            return []
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, cpu.read_cpu_sync, _now())

    async def read_disks(self) -> list[SensorReading]:
        if not self._config.disks.enabled:
            return []
        loop = asyncio.get_running_loop()
        devices = list(self._config.disks.devices)
        return await loop.run_in_executor(None, disks.read_disks_sync, devices, _now())

    async def read_all(self) -> list[SensorReading]:
        cpu_readings, disk_readings = await asyncio.gather(
            self.read_cpu(), self.read_disks()
        )
        return [*cpu_readings, *disk_readings]
