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
    ActionParam,
    ActionSpec,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.sensors.disks import SMARTCTL_REQUIREMENT
from sa_home_bot.sensors.lhm import lhm_problem
from sa_home_bot.sensors.power import (
    JOURNALCTL_REQUIREMENT,
    LAST_REQUIREMENT,
    read_power_events_sync,
    read_uptime_sync,
)
from sa_home_bot.utils.requirements import requirements_registry
from sa_home_bot.worker.queue import DedupQueue

SERVICE_NAME = "monitor"

ACTION_SCAN_NOW = "scan_now"
ACTION_DOWNTIME = "downtime"

# Границы страницы истории отключений (защита от абсурдных запросов).
DOWNTIME_DEFAULT_LIMIT = 10
DOWNTIME_MAX_LIMIT = 50


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
        # downtime — команда-представление (чтение с параметрами): фронтенды
        # не рисуют её кнопкой (у неё есть params), а зовут сами с offset/limit.
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=("temperature", "smart", "power"),
            actions=(
                ActionSpec(id=ACTION_SCAN_NOW, title="🔄 Скан датчиков"),
                ActionSpec(
                    id=ACTION_DOWNTIME,
                    title="⏻ Отключения",
                    params=(
                        ActionParam(name="offset", type="int", required=False),
                        ActionParam(name="limit", type="int", required=False),
                    ),
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        states = await self._store.get_all_states()
        # Диски — кэш SensorScanJob (см. Store.save_disk_summaries), не
        # живой опрос: smartctl/LHM на каждый запрос бота делали /status и
        # особенно веерный /swarm заметно медленными (живой баг 2026-07-18).
        disks = await self._store.get_disk_summaries() or []
        uptime, (outages, _) = await asyncio.gather(
            loop.run_in_executor(None, read_uptime_sync),
            loop.run_in_executor(None, read_power_events_sync, 0, 1),
        )

        cpu_cfg = self._settings.sensors.cpu
        disk_cfg = self._settings.sensors.disks
        # smartctl — только если мониторинг дисков включён (иначе не шумим
        # про программу, от которой ничего и не ждали); история отключений
        # (`last`, journalctl) — всегда, она не зависит от датчиков. journalctl
        # упоминается отдельно, только когда сам `last` в порядке — иначе оба
        # сводятся к одной проблеме (нет истории отключений / не та ОС).
        smartctl_problem = (
            requirements_registry.problem_for(SMARTCTL_REQUIREMENT) if disk_cfg.enabled else None
        )
        last_problem = requirements_registry.problem_for(LAST_REQUIREMENT)
        journal_problem = (
            None if last_problem else requirements_registry.problem_for(JOURNALCTL_REQUIREMENT)
        )
        requirements = [
            p
            for p in (
                smartctl_problem,
                last_problem,
                journal_problem,
                lhm_problem(self._settings.sensors.lhm.dll_path),
            )
            if p is not None
        ]
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
            "requirements": requirements,
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_SCAN_NOW:
            sensor_queued = await self._queue.put(SensorScanJob())
            smart_queued = await self._queue.put(SmartScanJob())
            return {"sensor_queued": sensor_queued, "smart_queued": smart_queued}
        if action == ACTION_DOWNTIME:
            return await self._downtime_page(args)
        # Сервер валидирует action по describe — сюда неизвестное не доходит.
        raise ValueError(f"необъявленное действие: {action}")

    async def _downtime_page(self, args: dict[str, Any]) -> dict[str, Any]:
        """Страница истории отключений ЭТОЙ машины (`last`+`journalctl`).

        Раньше бот читал журнал сам — работало только для своей машины;
        теперь история любой ноды доступна по протоколу («спроси любого»).
        """
        try:
            offset = max(0, int(args.get("offset", 0)))
        except (TypeError, ValueError):
            offset = 0
        try:
            limit = int(args.get("limit", DOWNTIME_DEFAULT_LIMIT))
        except (TypeError, ValueError):
            limit = DOWNTIME_DEFAULT_LIMIT
        limit = max(1, min(limit, DOWNTIME_MAX_LIMIT))

        loop = asyncio.get_running_loop()
        events, has_next = await loop.run_in_executor(
            None, read_power_events_sync, offset, limit
        )
        return {
            "events": [_outage_dict(e) for e in events],
            "offset": offset,
            "has_next": has_next,
        }
