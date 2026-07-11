"""NodeService — управление нодой по протоколу v0 (клиент — nodectl).

`get_state` — статус служб под супервизией, аптайм и presence пиров;
действия start/stop/restart объявлены в describe с параметром ``name``
и валидируются сервером по нему. Power-действия (выключить/перезагрузить/
усыпить машину) и ``restart_node`` (сама нода-супервизор, не путать с
рестартом службы) — умения роя без параметров: выполнение с задержкой,
чтобы ответ успел уйти клиенту.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from collections.abc import Callable
from typing import Any

from sa_home_bot import __version__
from sa_home_bot.node.peers import NodeRouter
from sa_home_bot.node.supervisor import Supervisor
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ActionParam,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
)
from sa_home_bot.runtime import Runtime
from sa_home_bot.utils.system import system_uptime_seconds

log = logging.getLogger(__name__)

SERVICE_NAME = "node"

ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_RESTART = "restart"

ACTION_RESTART_NODE = "restart_node"

ACTION_POWEROFF = "poweroff"
ACTION_REBOOT = "reboot"
ACTION_SUSPEND = "suspend"

RESTART_NODE_TITLE = "🔄 Перезапустить ноду"

# Пауза перед выполнением power-команды/само-рестарта: ответ и события
# должны успеть уйти.
POWER_DELAY_S = 1.0


def power_commands() -> dict[str, list[str]]:
    """Команды управления питанием текущей ОС (умение объявляется по факту)."""
    if sys.platform == "win32":
        return {
            ACTION_POWEROFF: ["shutdown", "/s", "/t", "5"],
            ACTION_REBOOT: ["shutdown", "/r", "/t", "5"],
            ACTION_SUSPEND: ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
        }
    return {
        ACTION_POWEROFF: ["systemctl", "poweroff"],
        ACTION_REBOOT: ["systemctl", "reboot"],
        ACTION_SUSPEND: ["systemctl", "suspend"],
    }


_POWER_TITLES = {
    ACTION_POWEROFF: "⏻ Выключить машину",
    ACTION_REBOOT: "🔃 Перезагрузить машину",
    ACTION_SUSPEND: "🌙 Усыпить машину",
}


async def _default_power_runner(argv: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        log.error(
            "Power-команда %s завершилась с кодом %s: %s",
            argv,
            proc.returncode,
            stderr.decode(errors="replace").strip(),
        )

class NodeService:
    def __init__(
        self,
        supervisor: Supervisor,
        router: NodeRouter | None = None,
        *,
        node_id: str = "",
        power_runner=None,
        restart_node: Callable[[], None] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._router = router
        self._node = node_id or socket.gethostname()
        self._runtime = Runtime()
        self._power = power_commands()
        self._power_runner = power_runner or _default_power_runner
        self._restart_node = restart_node

    def describe(self) -> ServiceDescription:
        # choices — имена служб под супервизией: фронтенд строит кнопку на
        # каждое значение, ничего не хардкодя.
        name_param = ActionParam(
            name="name",
            type="string",
            required=True,
            title="Служба",
            choices=tuple(self._supervisor.services),
        )
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=("supervisor", "power"),
            actions=(
                ActionSpec(id=ACTION_START, title="▶️ Запустить", params=(name_param,)),
                ActionSpec(id=ACTION_STOP, title="⏹ Остановить", params=(name_param,)),
                ActionSpec(id=ACTION_RESTART, title="🔄 Перезапустить", params=(name_param,)),
                *(
                    (ActionSpec(id=ACTION_RESTART_NODE, title=RESTART_NODE_TITLE),)
                    if self._restart_node is not None
                    else ()
                ),
                *(
                    ActionSpec(id=action, title=_POWER_TITLES[action])
                    for action in self._power
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        return {
            "node": self._node,
            "service": SERVICE_NAME,
            "version": __version__,
            "uptime_s": round(self._runtime.uptime_seconds(), 1),
            "system_uptime_s": system_uptime_seconds(),
            "services": [svc.to_dict() for svc in self._supervisor.services.values()],
            "peers": self._router.peers_state() if self._router is not None else [],
        }

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_RESTART_NODE:
            return self._schedule_restart_node()
        if action in self._power:
            return self._schedule_power(action)
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

    def _schedule_power(self, action: str) -> dict[str, Any]:
        argv = self._power[action]
        log.warning("Power-действие %s: выполняю %s через %.0f с", action, argv, POWER_DELAY_S)

        async def run() -> None:
            await asyncio.sleep(POWER_DELAY_S)
            await self._power_runner(argv)

        task = asyncio.create_task(run(), name=f"power-{action}")
        self._power_task = task  # ссылка, чтобы задачу не собрал GC
        return {"scheduled": action, "delay_s": POWER_DELAY_S}

    def _schedule_restart_node(self) -> dict[str, Any]:
        # describe() объявляет это действие только когда restart_node задан,
        # сервер валидирует action по describe — сюда без колбэка не дойти.
        assert self._restart_node is not None
        log.warning("Запрошен само-рестарт ноды через %.0f с", POWER_DELAY_S)

        async def run() -> None:
            await asyncio.sleep(POWER_DELAY_S)
            self._restart_node()

        task = asyncio.create_task(run(), name="restart-node")
        self._power_task = task  # ссылка, чтобы задачу не собрал GC
        return {"scheduled": ACTION_RESTART_NODE, "delay_s": POWER_DELAY_S}
