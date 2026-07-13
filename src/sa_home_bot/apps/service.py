"""AppsService — ServiceHandler службы apps (адаптер приложений).

Каждое приложение из конфига (`[apps] items`) — умение роя: индивидуальное
действие в describe (`id` приложения — карточка со статусом и ссылками) плюс
общие `start`/`stop`/`restart` с параметром `name` (выбор приложения) —
реальное управление systemd-юнитом, не только просмотр. Право на скилл в
подписке — `<id>@apps` (карточка) и `start@apps`/`stop@apps`/`restart@apps`
(управление). В систему ходит только эта служба, фронтенды — по протоколу
(правило «бот в систему не ходит»).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.config import AppConfig, Settings
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NEEDS_PRIVILEGE,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.utils.requirements import looks_like_permission_error

SERVICE_NAME = "apps"

# Статусы приложения (значение `systemctl is-active` юнита).
STATUS_ACTIVE = "active"
STATUS_UNKNOWN = "unknown"

ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_RESTART = "restart"
_MANAGE_ACTIONS = {
    ACTION_START: "▶️ Запустить",
    ACTION_STOP: "⏹ Остановить",
    ACTION_RESTART: "🔄 Перезапустить",
}


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


async def _run_systemctl(action: str, unit: str) -> None:
    """`sudo -n systemctl <action> <unit>` — `-n`: без интерактивного prompt,
    отказ без настроенного NOPASSWD-сниппета (`nodectl fix`) — и есть сигнал.
    """
    proc = await asyncio.create_subprocess_exec(
        "sudo",
        "-n",
        "systemctl",
        action,
        unit,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_raw = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_raw.decode(errors="replace").strip()
        if looks_like_permission_error(stderr) or "a password is required" in stderr.lower():
            raise ProtoError(
                ERR_NEEDS_PRIVILEGE,
                f"нужны права для управления {unit} — по SSH выполните: nodectl fix",
            )
        raise ProtoError(ERR_INTERNAL, f"systemctl {action} {unit} завершился ошибкой: {stderr}")


class AppsService:
    def __init__(self, settings: Settings) -> None:
        self._apps: dict[str, AppConfig] = {a.id: a for a in settings.apps.items}
        self._node = socket.gethostname()

    def describe(self) -> ServiceDescription:
        name_param = ActionParam(
            name="name",
            type="string",
            required=True,
            title="Приложение",
            choices=tuple(self._apps),
        )
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=tuple(self._apps),
            actions=(
                *(ActionSpec(id=app.id, title=app.title) for app in self._apps.values()),
                *(
                    ActionSpec(id=action, title=title, params=(name_param,))
                    for action, title in _MANAGE_ACTIONS.items()
                ),
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

    def _resolve(self, args: dict[str, Any]) -> AppConfig:
        name = str(args.get("name", ""))
        app = self._apps.get(name)
        if app is None:
            known = ", ".join(self._apps) or "нет приложений"
            raise ProtoError(ERR_BAD_REQUEST, f"нет такого приложения: {name!r} (есть: {known})")
        return app

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action in _MANAGE_ACTIONS:
            app = self._resolve(args)
            await _run_systemctl(action, app.unit)
            return await self._app_dict(app)
        app = self._apps.get(action)
        if app is None:
            # Сервер валидирует action по describe — сюда неизвестное не доходит.
            raise ValueError(f"необъявленное действие: {action}")
        return await self._app_dict(app)
