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
from sa_home_bot.config import SwarmNodeConfig
from sa_home_bot.node import update as node_update
from sa_home_bot.node.peers import NodeRouter, PeerLink
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import ASSIGNMENT_ARGS, EventEmitter, Supervisor
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_UNAVAILABLE,
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

ACTION_ASSIGN = "assign"
ACTION_UNASSIGN = "unassign"

ACTION_SWARM_JOIN = "swarm_join"  # входящее: сосед присоединяется к нам
ACTION_JOIN = "join"  # исходящее: мы присоединяемся к соседу (nodectl join)
EVENT_NODE_JOINED = "node_joined"

ACTION_RESTART_NODE = "restart_node"

ACTION_POWEROFF = "poweroff"
ACTION_REBOOT = "reboot"
ACTION_SUSPEND = "suspend"

ACTION_CHECK_UPDATE = "check_update"
ACTION_UPDATE = "update"
EVENT_UPDATE_FINISHED = "update_finished"

RESTART_NODE_TITLE = "🔄 Перезапустить ноду"
SWARM_JOIN_TITLE = "🤝 Присоединить ноду"
JOIN_TITLE = "🔗 Присоединиться к рою"
CHECK_UPDATE_TITLE = "🔍 Проверить обновления"
UPDATE_TITLE = "⬆️ Обновить"

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
        state: NodeState | None = None,
        state_path: str | None = None,
        local_service_endpoints: dict[str, str] | None = None,
        swarm_token: str = "",
        own_endpoint: str = "",
        emit: EventEmitter | None = None,
        update_source: str | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._router = router
        self._node = node_id or socket.gethostname()
        self._runtime = Runtime()
        self._power = power_commands()
        self._power_runner = power_runner or _default_power_runner
        self._restart_node = restart_node
        # assign/unassign: состояние ноды переживает рестарт через state_path
        # (см. node/state.py); без него — только в памяти этого процесса
        # (удобно для тестов и для ноды без настроенного каталога данных).
        self._state = state if state is not None else NodeState()
        self._state_path = state_path
        # Локальные службы со своим proto-сервером (monitor, apps) — при
        # assign() нужно ещё поднять PeerLink в router, не только процесс.
        # telegram-bot — клиент, сервера у него нет, сюда не входит.
        self._local_service_endpoints = local_service_endpoints or {}
        self._swarm_token = swarm_token
        # swarm_join: без своего TCP-адреса нечего давать соседям для обратной
        # связи — действие объявляется только когда есть куда стучаться.
        self._own_endpoint = own_endpoint
        self._emit = emit
        # Самообновление через pipx (не требует root — в отличие от
        # `nodectl fix`, можно звать прямо из этого процесса). None — ставили
        # не из git-репозитория (dev-чекаут и т.п.) — умение не объявляется.
        self._update_source = update_source
        self._updating = False
        self._last_update: dict[str, Any] | None = None

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
        assign_param = ActionParam(
            name="name",
            type="string",
            required=True,
            title="Назначение",
            choices=tuple(ASSIGNMENT_ARGS),
        )
        return ServiceDescription(
            info=ServiceInfo(node=self._node, service=SERVICE_NAME, version=__version__),
            capabilities=("supervisor", "power"),
            actions=(
                ActionSpec(id=ACTION_START, title="▶️ Запустить", params=(name_param,)),
                ActionSpec(id=ACTION_STOP, title="⏹ Остановить", params=(name_param,)),
                ActionSpec(id=ACTION_RESTART, title="🔄 Перезапустить", params=(name_param,)),
                ActionSpec(id=ACTION_ASSIGN, title="➕ Назначить", params=(assign_param,)),
                ActionSpec(id=ACTION_UNASSIGN, title="➖ Снять", params=(name_param,)),
                *(
                    (
                        ActionSpec(
                            id=ACTION_SWARM_JOIN,
                            title=SWARM_JOIN_TITLE,
                            params=(
                                ActionParam(name="node_id", title="Id ноды"),
                                ActionParam(name="endpoint", title="Endpoint"),
                            ),
                        ),
                        ActionSpec(
                            id=ACTION_JOIN,
                            title=JOIN_TITLE,
                            params=(ActionParam(name="endpoint", title="Endpoint соседа"),),
                        ),
                    )
                    if self._own_endpoint
                    else ()
                ),
                *(
                    (ActionSpec(id=ACTION_RESTART_NODE, title=RESTART_NODE_TITLE),)
                    if self._restart_node is not None
                    else ()
                ),
                *(
                    (
                        ActionSpec(id=ACTION_CHECK_UPDATE, title=CHECK_UPDATE_TITLE),
                        ActionSpec(id=ACTION_UPDATE, title=UPDATE_TITLE),
                    )
                    if self._update_source is not None
                    else ()
                ),
                *(
                    ActionSpec(id=action, title=_POWER_TITLES[action])
                    for action in self._power
                ),
            ),
        )

    async def get_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "node": self._node,
            "service": SERVICE_NAME,
            "version": __version__,
            "uptime_s": round(self._runtime.uptime_seconds(), 1),
            "system_uptime_s": system_uptime_seconds(),
            "services": [svc.to_dict() for svc in self._supervisor.services.values()],
            "peers": self._router.peers_state() if self._router is not None else [],
        }
        if self._update_source is not None:
            installed = node_update.installed_version()
            state["update"] = {
                "running": __version__,
                "installed": installed,
                "restart_required": installed is not None and installed != __version__,
                "last": self._last_update,
            }
        return state

    async def run_command(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == ACTION_RESTART_NODE:
            return self._schedule_restart_node()
        if action in self._power:
            return self._schedule_power(action)
        if action == ACTION_SWARM_JOIN:
            return await self._swarm_join(args)
        if action == ACTION_JOIN:
            return await self.join(str(args.get("endpoint", "")))
        if action == ACTION_CHECK_UPDATE:
            return await self._check_update()
        if action == ACTION_UPDATE:
            return await self._update()
        name = str(args.get("name", ""))
        if action == ACTION_ASSIGN:
            return await self._assign(name)
        if action == ACTION_UNASSIGN:
            return await self._unassign(name)
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

    async def _assign(self, name: str) -> dict[str, Any]:
        """Назначить службу в рантайме: поднять процесс + (если есть свой
        сокет — monitor/apps) линк в router, персистентно (переживает рестарт
        ноды через state_path). Идемпотентно — повторный assign не дублирует.
        """
        try:
            svc = self._supervisor.assign(name)
        except ValueError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc
        await svc.start()
        endpoint = self._local_service_endpoints.get(name)
        already_linked = self._router is not None and name in self._router.local_services
        if self._router is not None and endpoint is not None and not already_linked:
            link = PeerLink(name, endpoint, token=self._swarm_token)
            await self._router.add_local_service(name, link)
        if name not in self._state.assignments:
            self._state.assignments.append(name)
            self._save_state()
        return {"service": svc.to_dict()}

    async def _unassign(self, name: str) -> dict[str, Any]:
        if self._supervisor.get(name) is None:
            known = ", ".join(self._supervisor.services) or "нет служб"
            raise ProtoError(ERR_BAD_REQUEST, f"нет такой службы: {name!r} (есть: {known})")
        await self._supervisor.unassign(name)
        if self._router is not None:
            await self._router.remove_local_service(name)
        if name in self._state.assignments:
            self._state.assignments.remove(name)
            self._save_state()
        return {"unassigned": name}

    def _save_state(self) -> None:
        if self._state_path is not None:
            self._state.save(self._state_path)

    async def _swarm_join(self, args: dict[str, Any]) -> dict[str, Any]:
        """Принять присоединение соседа: узнать о нём, вернуть полный граф
        известных пиров (включая себя) за один round-trip — присоединяющийся
        сразу может связаться со всеми, без цепочки отдельных запросов.
        """
        caller_id = str(args.get("node_id", ""))
        caller_endpoint = str(args.get("endpoint", ""))
        if not caller_id or not caller_endpoint:
            raise ProtoError(ERR_BAD_REQUEST, "swarm_join требует node_id и endpoint")
        if caller_id == self._node:
            raise ProtoError(ERR_BAD_REQUEST, "нода не может присоединиться сама к себе")

        if self._router is not None:
            existing = self._router.peers.get(caller_id)
            if existing is None or str(existing.endpoint) != caller_endpoint:
                if existing is not None:
                    await self._router.remove_peer(caller_id)
                link = PeerLink(caller_id, caller_endpoint, token=self._swarm_token)
                await self._router.add_peer(link)
            self._remember_peer(caller_id, caller_endpoint)

        if self._emit is not None:
            await self._emit(EVENT_NODE_JOINED, {"node_id": caller_id, "endpoint": caller_endpoint})

        peers: list[dict[str, Any]] = [
            {"id": self._node, "endpoint": self._own_endpoint, "alive": True}
        ]
        if self._router is not None:
            peers += self._router.peers_state()
        return {"peers": peers}

    def _remember_peer(self, node_id: str, endpoint: str) -> None:
        """Персистентный справочник пиров (не полный конфиг соседа — только
        id+endpoint, см. node/state.py)."""
        others = [p for p in self._state.peers if p.id != node_id]
        self._state.peers = [*others, SwarmNodeConfig(id=node_id, endpoint=endpoint)]
        self._save_state()

    async def join(self, endpoint: str) -> dict[str, Any]:
        """Присоединиться к рою через уже существующую ноду: разовый запрос
        (не постоянный `PeerLink`) `swarm_join`, из ответа — полный граф
        пиров, связаться со всеми напрямую («один seed → полный mesh»).

        Тот же путь и для установки (`node/app.py` при первом старте с
        `[swarm].join`), и для консоли (`nodectl join`) — по инварианту
        «сначала действие ноды» ни бот, ни установка не обходят протокол.
        """
        if not endpoint:
            raise ProtoError(ERR_BAD_REQUEST, "join требует endpoint")
        client = ProtoClient(endpoint, token=self._swarm_token)
        try:
            await client.connect()
            result = await client.command(
                "swarm_join", {"node_id": self._node, "endpoint": self._own_endpoint}
            )
        except (ConnectionError, OSError, TimeoutError, ProtoError) as exc:
            raise ProtoError(ERR_UNAVAILABLE, f"сосед {endpoint} недоступен: {exc}") from exc
        finally:
            await client.close()

        added: list[str] = []
        if self._router is not None:
            for peer in result.get("peers", []):
                pid, peer_endpoint = peer.get("id"), peer.get("endpoint")
                if not pid or not peer_endpoint or pid == self._node or pid in self._router.peers:
                    continue
                link = PeerLink(pid, peer_endpoint, token=self._swarm_token)
                await self._router.add_peer(link)
                self._remember_peer(pid, peer_endpoint)
                added.append(pid)
        return {"joined_via": endpoint, "peers_added": added}

    async def _check_update(self) -> dict[str, Any]:
        """Только посмотреть: что работает, что на диске, что в репозитории.
        Ничего не переустанавливает."""
        assert self._update_source is not None
        latest = await node_update.latest_tag(self._update_source)
        if latest is None:
            raise ProtoError(ERR_INTERNAL, "не удалось проверить обновления (сеть?)")
        return {
            "repo": self._update_source,
            "running": __version__,
            "installed": node_update.installed_version(),
            # latest_tag() отдаёт git-тег как есть ("vX.Y.Z") — а
            # installed_version()/__version__ без префикса ("X.Y.Z", PEP 440);
            # без lstrip сравнение installed == latest никогда бы не совпадало
            # (баг 2026-07-17: update считал себя устаревшим на каждой
            # свежепоставленной версии и переустанавливался вхолостую).
            "latest": latest.lstrip("v"),
        }

    async def _update(self) -> dict[str, Any]:
        """Подтянуть последний тег через pipx — БЕЗ рестарта процесса.

        Файлы на диске обновляются сразу; уже загруженный в память код
        продолжает работать по-старому, пока человек не выполнит
        restart_node — get_state().update.restart_required честно скажет,
        когда это нужно.
        """
        assert self._update_source is not None
        latest = await node_update.latest_tag(self._update_source)  # тег с "v" — нужен git-ref
        if latest is None:
            raise ProtoError(ERR_INTERNAL, "не удалось проверить обновления (сеть?)")
        target_version = latest.lstrip("v")  # для сравнения/отображения — см. _check_update
        installed = node_update.installed_version()
        if installed == target_version:
            return {"up_to_date": True, "version": target_version}
        if self._updating:
            raise ProtoError(ERR_BAD_REQUEST, "обновление уже выполняется")
        self._schedule_update(latest, target_version)
        return {"scheduled": True, "target_version": target_version}

    def _schedule_update(self, git_ref: str, target_version: str) -> None:
        assert self._update_source is not None
        self._updating = True
        repo = self._update_source

        async def run() -> None:
            try:
                ok, output = await node_update.pipx_reinstall(repo, git_ref)
            except Exception as exc:  # noqa: BLE001 — фон не должен уронить ноду
                ok, output = False, str(exc)
            finally:
                self._updating = False
            if ok:
                log.warning("Обновление до %s установлено — нужен restart_node", target_version)
            else:
                log.error("Обновление до %s не удалось: %s", target_version, output)
            self._last_update = {
                "ok": ok,
                "version": target_version,
                "error": None if ok else output,
            }
            if self._emit is not None:
                await self._emit(EVENT_UPDATE_FINISHED, self._last_update)

        task = asyncio.create_task(run(), name="node-update")
        self._update_task = task  # ссылка, чтобы задачу не собрал GC

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
