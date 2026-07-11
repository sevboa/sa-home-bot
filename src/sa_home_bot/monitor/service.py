"""MonitorService — реализация ServiceHandler для службы monitor.

`get_state` отдаёт сырые данные (здоровье, диски, аптайм, статистика прогонов),
рендер в человекочитаемый текст — забота фронтенда (бота). Действия объявляются
в `describe` — фронтенды строят UI по этому списку, ничего не хардкодя.
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import HealthState, PowerEvent
from sa_home_bot.jobs.scan import SensorScanJob
from sa_home_bot.jobs.smart import SmartScanJob
from sa_home_bot.proto.messages import (
    ActionSpec,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.sensors.disks import SMARTCTL_REQUIREMENT, read_disk_summaries_sync
from sa_home_bot.sensors.power import read_power_events_sync, read_uptime_sync
from sa_home_bot.worker.queue import DedupQueue

SERVICE_NAME = "monitor"

ACTION_SCAN_NOW = "scan_now"


def _health_dict(state: HealthState) -> dict[str, Any]:
    return {
        "component_id": state.component_id,
        "kind": state.kind,
        "label": state.label,
        "status": state.status,
        "temperature_c": state.temperature_c,
        "alerting_since": state.alerting_since.isoformat() if state.alerting_since else None,
    }


def _outage_dict(event: PowerEvent | None) -> dict[str, Any] | None:
    if event is None:
        return None
    downtime = event.downtime
    return {
        "kind": event.kind,
        "boot_at": event.boot_at.isoformat(),
        "down_at": event.down_at.isoformat() if event.down_at else None,
        "up_at": event.up_at.isoformat() if event.up_at else None,
        "down_approx": event.down_approx,
        "downtime_s": downtime.total_seconds() if downtime is not None else None,
    }


class MonitorService:
    def __init__(self, settings: Settings, store: Store, queue: DedupQueue) -> None:
        self._settings = settings
        self._store = store
        self._queue = queue
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=("temperature", "smart", "power"),
            actions=(ActionSpec(id=ACTION_SCAN_NOW, title="🔄 Скан датчиков"),),
        )

    async def get_state(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        states = await self._store.get_all_states()
        health_map = await self._store.get_smart_health_map()
        uptime = await loop.run_in_executor(None, read_uptime_sync)
        disks = await loop.run_in_executor(
            None,
            read_disk_summaries_sync,
            list(self._settings.sensors.disks.devices),
            health_map,
        )
        outages, _ = await loop.run_in_executor(None, read_power_events_sync, 0, 1)

        cpu_cfg = self._settings.sensors.cpu
        disk_cfg = self._settings.sensors.disks
        missing_requirements = []
        if disk_cfg.enabled and not SMARTCTL_REQUIREMENT.available():
            missing_requirements.append(SMARTCTL_REQUIREMENT.install_hint())
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "now": datetime.now(tz=UTC).isoformat(),
            "uptime_s": uptime.total_seconds() if uptime is not None else None,
            "health": [_health_dict(s) for s in states],
            "disks": [asdict(d) for d in disks],
            "last_outage": _outage_dict(outages[0] if outages else None),
            "job_counts": await self._store.job_run_counts(),
            "recent_runs": await self._store.recent_job_runs(limit=8),
            "thresholds": {
                "cpu": {"warn_c": cpu_cfg.warn_c, "crit_c": cpu_cfg.crit_c},
                "disk": {"warn_c": disk_cfg.warn_c, "crit_c": disk_cfg.crit_c},
            },
            "missing_requirements": missing_requirements,
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_SCAN_NOW:
            sensor_queued = await self._queue.put(SensorScanJob())
            smart_queued = await self._queue.put(SmartScanJob())
            return {"sensor_queued": sensor_queued, "smart_queued": smart_queued}
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")
