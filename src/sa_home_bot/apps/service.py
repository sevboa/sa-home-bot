"""AppsService — ServiceHandler службы apps (адаптер приложений).

Каждое приложение из конфига (`[apps] items`) — умение роя: действие в
describe с id приложения. `command <id>` и `get_state` отвечают состоянием
systemd-юнита и ссылками на веб-морду; в систему ходит только эта служба,
фронтенды — по протоколу (правило «бот в систему не ходит»).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import AppConfig, Settings
from sa_home_bot.proto.messages import ActionSpec, ServiceDescription, ServiceInfo

SERVICE_NAME = "apps"

# Статусы приложения (значение `systemctl is-active` юнита).
STATUS_ACTIVE = "active"
STATUS_UNKNOWN = "unknown"


async def read_unit_status(unit: str) -> str:
    """`systemctl is-active <unit>` → active/inactive/failed/… (unknown при сбое)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl",
            "is-active",
            unit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return STATUS_UNKNOWN
    status = stdout.decode().strip()
    return status or STATUS_UNKNOWN


class AppsService:
    def __init__(self, settings: Settings) -> None:
        self._apps: dict[str, AppConfig] = {a.id: a for a in settings.apps.items}
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=tuple(self._apps),
            actions=tuple(
                ActionSpec(id=app.id, title=app.title) for app in self._apps.values()
            ),
        )

    async def _app_dict(self, app: AppConfig) -> dict[str, Any]:
        return {
            "id": app.id,
            "title": app.title,
            "unit": app.unit,
            "status": await read_unit_status(app.unit),
            "urls": list(app.urls),
        }

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "apps": [await self._app_dict(app) for app in self._apps.values()],
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        app = self._apps.get(action)
        if app is None:
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
            raise ValueError(f"необъявленное действие: {action}")
        return await self._app_dict(app)
