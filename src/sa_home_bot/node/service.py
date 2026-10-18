from __future__ import annotations

import asyncio
import logging
import socket
import sys
from collections.abc import Callable, Sequence
from typing import Any

from sa_home_bot import __version__, wol
from sa_home_bot.node import assignments as assignments_mod
from sa_home_bot.node import kind as node_kinds
from sa_home_bot.node import update as node_update
from sa_home_bot.node.lease import LeaseManager
from sa_home_bot.node.peers import LOCAL_HEARTBEAT_TIMEOUT_S, NodeRouter, PeerLink
from sa_home_bot.node.replication import (
    ACTION_GET_INSTANCE_CONFIG,
    ACTION_LIST_INSTANCES,
    ConfigReplicator,
)
from sa_home_bot.node.state import NodeState
from sa_home_bot.node.supervisor import EventEmitter, Supervisor
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import merge_endpoints
from sa_home_bot.proto.messages import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    MSG_COMMAND,
    ActionParam,
    ActionSpec,
    Address,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
    make_request,
)
from sa_home_bot.proto.server import undeclared_action
from sa_home_bot.runtime import Runtime
from sa_home_bot.services import registry
from sa_home_bot.utils import ssh_sessions
from sa_home_bot.utils.system import system_uptime_seconds

log = logging.getLogger(__name__)

SERVICE_NAME = "node"

ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_RESTART = "restart"

ACTION_ASSIGN = "assign"
ACTION_UNASSIGN = "unassign"

ACTION_SWARM_JOIN = "swarm_join"
ACTION_JOIN = "join"
EVENT_NODE_JOINED = "node_joined"

ACTION_RESTART_NODE = "restart_node"

ACTION_POWEROFF = "poweroff"
ACTION_REBOOT = "reboot"
ACTION_SUSPEND = "suspend"

ACTION_CLOSE_SSH_SESSIONS = "close_ssh_sessions"
CLOSE_SSH_SESSIONS_TITLE = "🔌 Закрыть SSH+tmux и выключить"
EVENT_IDLE_POWER_BLOCKED = "idle_power_blocked"

ACTION_CHECK_UPDATE = "check_update"
ACTION_UPDATE = "update"
EVENT_UPDATE_FINISHED = "update_finished"

EVENT_RESTART_APPLIED = "restart_applied"

ACTION_SEND_WOL = "send_wol"

ACTION_REPORT_READY = "report_ready"

ACTION_TRIGGER_PEERS = "trigger_peers"

RESTART_NODE_TITLE = "🔄 Перезапустить ноду"
SWARM_JOIN_TITLE = "🤝 Присоединить ноду"
JOIN_TITLE = "🔗 Присоединиться к рою"
CHECK_UPDATE_TITLE = "🔍 Проверить обновления"
UPDATE_TITLE = "⬆️ Обновить"
SEND_WOL_TITLE = "📡 Отправить Wake-on-LAN"

POWER_DELAY_S = 1.0


def power_commands() -> dict[str, list[str]]:
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
        advertise: Callable[[], list[str]] | None = None,
        emit: EventEmitter | None = None,
        update_source: str | None = None,
        node_kind: str = node_kinds.KIND_SERVER,
        power_controllable: bool | None = None,
        idle_poweroff: bool = False,
        replicator: ConfigReplicator | None = None,
        lease: LeaseManager | None = None,
        make_peer_link: Callable[[str, str | Sequence[str]], PeerLink] | None = None,
        make_local_link: Callable[[str, str | Sequence[str]], PeerLink] | None = None,
    ) -> None:
        self._replicator = replicator
        self._lease = lease
        self._supervisor = supervisor
        self._router = router
        self._node = node_id or socket.gethostname()
        self._kind = node_kind
        self._traits = node_kinds.traits_for(node_kind)
        self._runtime = Runtime()
        controllable = (
            self._traits.power_controllable
            if power_controllable is None
            else power_controllable
        )
        self._power = power_commands() if controllable else {}
        self._power_runner = power_runner or _default_power_runner
        self._idle_poweroff = idle_poweroff and controllable
        self._restart_node = restart_node
        self._state = state if state is not None else NodeState()
        self._state_path = state_path
        self._local_service_endpoints = local_service_endpoints or {}
        self._swarm_token = swarm_token
        self._make_peer_link = make_peer_link or (
            lambda pid, ep: PeerLink(pid, ep, token=swarm_token, self_node=self._node)
        )
        self._make_local_link = make_local_link or (
            lambda name, ep: PeerLink(
                name, ep, token=swarm_token, heartbeat_timeout=LOCAL_HEARTBEAT_TIMEOUT_S
            )
        )
        self._own_endpoint = own_endpoint
        self._advertise = advertise
        self._emit = emit
        self._update_source = update_source
        self._updating = False
        self._last_update: dict[str, Any] | None = None
        self._discovery_state: Callable[[], dict[str, Any]] | None = None

    def attach_discovery(self, state: Callable[[], dict[str, Any]]) -> None:
        self._discovery_state = state

    def describe(self) -> ServiceDescription:
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
            choices=registry.assignable_names(),
        )
        mac_param = ActionParam(name="mac", type="string", required=True, title="MAC цели")
        return ServiceDescription(
            info=ServiceInfo(
                node=self._node,
                service=SERVICE_NAME,
                version=__version__,
                node_kind=self._kind,
                wake=self._local_wake_payload(),
                endpoints=tuple(self._advertised()),
            ),
            capabilities=("supervisor", "power"),
            actions=(
                ActionSpec(id=ACTION_START, title="▶️ Запустить", params=(name_param,)),
                ActionSpec(id=ACTION_STOP, title="⏹ Остановить", params=(name_param,)),
                ActionSpec(id=ACTION_RESTART, title="🔄 Перезапустить", params=(name_param,)),
                ActionSpec(id=ACTION_ASSIGN, title="➕ Назначить", params=(assign_param,)),
                ActionSpec(id=ACTION_UNASSIGN, title="➖ Снять", params=(name_param,)),
                ActionSpec(id=ACTION_SEND_WOL, title=SEND_WOL_TITLE, params=(mac_param,)),
                ActionSpec(
                    id=ACTION_REPORT_READY,
                    title="📶 Готовность службы",
                    params=(
                        ActionParam(name="name", required=True, title="Слот службы"),
                        ActionParam(name="ready", type="bool", required=True, title="Готова"),
                    ),
                ),
                ActionSpec(
                    id=ACTION_TRIGGER_PEERS,
                    title="📡 Разослать команду службе на всех нодах",
                    params=(
                        ActionParam(name="service", required=True, title="Служба"),
                        ActionParam(name="action", required=True, title="Действие"),
                    ),
                ),
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
                *(
                    (
                        ActionSpec(
                            id=ACTION_CLOSE_SSH_SESSIONS, title=CLOSE_SSH_SESSIONS_TITLE
                        ),
                    )
                    if ACTION_POWEROFF in self._power
                    else ()
                ),
                *(
                    (
                        ActionSpec(
                            id=ACTION_LIST_INSTANCES,
                            title="📦 Пакеты настроек",
                            params=(
                                ActionParam(name="service", required=True, title="Служба"),
                            ),
                        ),
                        ActionSpec(
                            id=ACTION_GET_INSTANCE_CONFIG,
                            title="📦 Забрать пакет настроек",
                            params=(
                                ActionParam(name="service", required=True, title="Служба"),
                                ActionParam(name="instance", required=True, title="Инстанс"),
                            ),
                        ),
                    )
                    if self._replicator is not None
                    else ()
                ),
            ),
        )

    def _advertised(self) -> list[str]:
        live = self._advertise() if self._advertise is not None else []
        if live:
            return live
        return [self._own_endpoint] if self._own_endpoint else []

    @staticmethod
    def _local_wake_payload() -> dict[str, str] | None:
        info = wol.detect_local_wake_info()
        if info is None:
            return None
        return {"mac": info.mac, "ip": info.ip, "broadcast": info.broadcast}

    def _services_state(self) -> list[dict[str, Any]]:
        services = [svc.to_dict() for svc in self._supervisor.services.values()]
        if self._router is None:
            return services
        supervised = {svc.assignment.service for svc in self._supervisor.services.values()}
        for name, link in self._router.local_services.items():
            if name in supervised:
                continue
            services.append(
                {
                    "name": name,
                    "status": "running" if link.alive else "stopped",
                    "pid": None,
                    "restarts": 0,
                    "last_exit_code": None,
                    "started_at": None,
                    "service": name,
                    "instance": None,
                    "role": None,
                    "external": True,
                }
            )
        return services

    async def get_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "node": self._node,
            "service": SERVICE_NAME,
            "version": __version__,
            "kind": self._kind,
            "instances": (
                self._replicator.local_revisions() if self._replicator is not None else []
            ),
            "singletons": self._lease.local_state() if self._lease is not None else {},
            "uptime_s": round(self._runtime.uptime_seconds(), 1),
            "system_uptime_s": system_uptime_seconds(),
            "services": self._services_state(),
            "peers": self._router.peers_state() if self._router is not None else [],
            "wake": self._local_wake_payload(),
        }
        if self._discovery_state is not None:
            state["discovery"] = self._discovery_state()
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
        if action == ACTION_CLOSE_SSH_SESSIONS:
            return await self._close_ssh_sessions()
        if action == ACTION_SWARM_JOIN:
            return await self._swarm_join(args)
        if action == ACTION_JOIN:
            return await self.join(str(args.get("endpoint", "")))
        if action == ACTION_CHECK_UPDATE:
            return await self._check_update()
        if action == ACTION_UPDATE:
            return await self._update()
        if action == ACTION_SEND_WOL:
            return self._send_wol(args)
        if action == ACTION_LIST_INSTANCES:
            return self._list_instances(args)
        if action == ACTION_GET_INSTANCE_CONFIG:
            return self._get_instance_config(args)
        if action == ACTION_REPORT_READY:
            return self._report_ready(args)
        if action == ACTION_TRIGGER_PEERS:
            return await self._trigger_peers(args)
        name = str(args.get("name", ""))
        if action == ACTION_ASSIGN:
            return await self._assign(name)
        if action == ACTION_UNASSIGN:
            return await self._unassign(name)
        svc = self._supervisor.get(name)
        if svc is None:
            raise self._no_such_service(name)
        if action == ACTION_START:
            await svc.start()
        elif action == ACTION_STOP:
            await svc.stop()
        elif action == ACTION_RESTART:
            await svc.restart()
        else:
            raise undeclared_action(action)
        return {"service": svc.to_dict()}

    def _no_such_service(self, name: str) -> ProtoError:
        known = ", ".join(self._supervisor.services) or "нет служб"
        return ProtoError(ERR_BAD_REQUEST, f"нет такой службы: {name!r} (есть: {known})")

    async def _assign(self, name: str) -> dict[str, Any]:
        try:
            assignment = assignments_mod.parse(name)
            svc = self._supervisor.assign(name)
        except ValueError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc
        spec = registry.spec(assignment.service)
        if spec is None or not spec.singleton:
            await svc.start()
        service = assignment.service
        endpoint = self._local_service_endpoints.get(service)
        already_linked = self._router is not None and service in self._router.local_services
        if self._router is not None and endpoint is not None and not already_linked:
            link = self._make_local_link(service, endpoint)
            await self._router.add_local_service(service, link)
        if name not in self._state.assignments:
            self._state.assignments.append(name)
            self._save_state()
        return {"service": svc.to_dict()}

    async def _unassign(self, name: str) -> dict[str, Any]:
        key = assignments_mod.key_or_raw(name)
        if self._supervisor.get(key) is None:
            raise self._no_such_service(name)
        service = self._supervisor.services[key].assignment.service
        await self._supervisor.unassign(key)
        if self._router is not None:
            await self._router.remove_local_service(service)
        for item in list(self._state.assignments):
            if assignments_mod.key_or_raw(item) == key:
                self._state.assignments.remove(item)
                self._save_state()
        return {"unassigned": name}

    def _list_instances(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._replicator is None:
            raise ProtoError(ERR_BAD_REQUEST, "на этой ноде нет каталога пакетов настроек")
        service = str(args.get("service", ""))
        revisions = [
            meta for meta in self._replicator.local_revisions() if meta["service"] == service
        ]
        return {"service": service, "instances": revisions}

    def _get_instance_config(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._replicator is None:
            raise ProtoError(ERR_BAD_REQUEST, "на этой ноде нет каталога пакетов настроек")
        service = str(args.get("service", ""))
        instance = str(args.get("instance", ""))
        payload = self._replicator.package_payload(service, instance)
        if payload is None:
            raise ProtoError(
                ERR_BAD_REQUEST, f"нет пакета настроек {service}@{instance} на ноде {self._node}"
            )
        return payload

    def _report_ready(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", ""))
        if self._supervisor.get(name) is None:
            raise self._no_such_service(name)
        ready = bool(args.get("ready", True))
        if self._lease is not None:
            self._lease.set_ready(name, ready)
        return {"name": name, "ready": ready}

    async def _trigger_peers(self, args: dict[str, Any]) -> dict[str, Any]:
        service = str(args.get("service", ""))
        if not service:
            raise ProtoError(ERR_BAD_REQUEST, "не передан service")
        action = str(args.get("action", ""))
        if not action:
            raise ProtoError(ERR_BAD_REQUEST, "не передан action")
        call_args = args.get("args") or {}
        if not isinstance(call_args, dict):
            raise ProtoError(ERR_BAD_REQUEST, "args должен быть объектом")
        timeout_s = float(args.get("timeout_s") or 5.0)

        node_ids = [self._node]
        if self._router is not None:
            node_ids += [p["id"] for p in self._router.peers_state() if p["alive"]]

        async def dispatch_one(node_id: str) -> tuple[str, str]:
            if self._router is None:
                return node_id, "unreachable"
            env = make_request(
                MSG_COMMAND,
                {"action": action, "args": call_args},
                dst=Address(node=node_id, service=service),
                timeout_s=timeout_s,
            )
            try:
                response = await self._router.route(env)
            except ProtoError as exc:
                return node_id, "skipped" if exc.code == ERR_UNKNOWN_DST else "unreachable"
            if response is None or response.ok is False:
                code = response.error_code() if response is not None else None
                return node_id, "skipped" if code == ERR_UNKNOWN_DST else "unreachable"
            return node_id, "dispatched"

        outcome: dict[str, list[str]] = {"dispatched": [], "skipped": [], "unreachable": []}
        for node_id, status in await asyncio.gather(*(dispatch_one(n) for n in node_ids)):
            outcome[status].append(node_id)
        return outcome

    def _send_wol(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            mac = wol.normalize_mac(str(args.get("mac", "")))
        except ValueError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc
        info = wol.detect_local_wake_info()
        try:
            wol.send_magic_packet(mac, bind_ip=info.ip if info is not None else "")
        except OSError as exc:
            raise ProtoError(ERR_INTERNAL, f"не удалось отправить magic packet: {exc}") from exc
        log.warning("Wake-on-LAN: %s разослала magic packet для %s", self._node, mac)
        return {"sent": True, "mac": mac}

    def _save_state(self) -> None:
        if self._state_path is not None:
            self._state.save(self._state_path)

    async def _swarm_join(self, args: dict[str, Any]) -> dict[str, Any]:
        caller_id = str(args.get("node_id", ""))
        caller_endpoint = str(args.get("endpoint", ""))
        caller_endpoints = merge_endpoints(caller_endpoint, args.get("endpoints"))
        if not caller_id or not caller_endpoint:
            raise ProtoError(ERR_BAD_REQUEST, "swarm_join требует node_id и endpoint")
        if caller_id == self._node:
            raise ProtoError(ERR_BAD_REQUEST, "нода не может присоединиться сама к себе")

        if self._router is not None:
            existing = self._router.peers.get(caller_id)
            if existing is None or existing.endpoints != caller_endpoints:
                if existing is not None:
                    await self._router.remove_peer(caller_id)
                link = self._make_peer_link(caller_id, caller_endpoints)
                await self._router.add_peer(link)
            self._state.remember_peer(caller_id, caller_endpoint, caller_endpoints)
            self._save_state()

        if self._emit is not None:
            await self._emit(
                EVENT_NODE_JOINED,
                {
                    "node_id": caller_id,
                    "endpoint": caller_endpoint,
                    "endpoints": caller_endpoints,
                },
            )

        advertised = self._advertised()
        peers: list[dict[str, Any]] = [
            {
                "id": self._node,
                "endpoint": advertised[0] if advertised else self._own_endpoint,
                "endpoints": advertised,
                "alive": True,
            }
        ]
        if self._router is not None:
            peers += self._router.peers_state()
        return {"peers": peers}

    async def join(self, endpoint: str) -> dict[str, Any]:
        if not endpoint:
            raise ProtoError(ERR_BAD_REQUEST, "join требует endpoint")
        advertised = self._advertised()
        client = ProtoClient(endpoint, token=self._swarm_token)
        try:
            await client.connect()
            result = await client.command(
                "swarm_join",
                {
                    "node_id": self._node,
                    "endpoint": advertised[0] if advertised else self._own_endpoint,
                    "endpoints": advertised,
                },
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
                peer_endpoints = merge_endpoints(peer_endpoint, peer.get("endpoints"))
                link = self._make_peer_link(pid, peer_endpoints)
                await self._router.add_peer(link)
                self._state.remember_peer(pid, peer_endpoint, peer_endpoints)
                self._save_state()
                added.append(pid)
        return {"joined_via": endpoint, "peers_added": added}

    async def _check_update(self) -> dict[str, Any]:
        assert self._update_source is not None
        latest = await node_update.latest_tag(self._update_source)
        if latest is None:
            raise ProtoError(ERR_INTERNAL, "не удалось проверить обновления (сеть?)")
        return {
            "repo": self._update_source,
            "running": __version__,
            "installed": node_update.installed_version(),
            "latest": latest.lstrip("v"),
        }

    async def _update(self) -> dict[str, Any]:
        assert self._update_source is not None
        latest = await node_update.latest_tag(self._update_source)
        if latest is None:
            raise ProtoError(ERR_INTERNAL, "не удалось проверить обновления (сеть?)")
        target_version = latest.lstrip("v")
        installed = node_update.installed_version()
        if installed == target_version:
            return {
                "up_to_date": True,
                "version": target_version,
                "restart_required": __version__ != target_version,
            }
        if self._updating:
            raise ProtoError(ERR_BAD_REQUEST, "обновление уже выполняется")
        if sys.platform == "win32":
            return self._schedule_windows_update(target_version)
        self._schedule_update(latest, target_version)
        return {"scheduled": True, "target_version": target_version}

    def _schedule_windows_update(self, target_version: str) -> dict[str, Any]:
        self._updating = True

        async def run() -> None:
            try:
                ok, output = await node_update.trigger_scheduled_task()
            except Exception as exc:  # noqa: BLE001
                ok, output = False, str(exc)
            finally:
                self._updating = False
            if ok:
                log.warning("Задача автообновления запущена — служба скоро перезапустится сама")
                return
            log.error("Не удалось запустить задачу автообновления: %s", output)
            self._last_update = {"ok": False, "version": target_version, "error": output}
            if self._emit is not None:
                await self._emit(EVENT_UPDATE_FINISHED, self._last_update)

        task = asyncio.create_task(run(), name="node-update-win")
        self._update_task = task
        return {"scheduled": True, "target_version": target_version, "via": "scheduled_task"}

    def _schedule_update(self, git_ref: str, target_version: str) -> None:
        assert self._update_source is not None
        self._updating = True
        repo = self._update_source

        async def run() -> None:
            try:
                ok, output = await node_update.pipx_reinstall(repo, git_ref)
            except Exception as exc:  # noqa: BLE001
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
        self._update_task = task

    def _schedule_power(self, action: str) -> dict[str, Any]:
        argv = self._power[action]
        log.warning("Power-действие %s: выполняю %s через %.0f с", action, argv, POWER_DELAY_S)

        async def run() -> None:
            await asyncio.sleep(POWER_DELAY_S)
            await self._power_runner(argv)

        task = asyncio.create_task(run(), name=f"power-{action}")
        self._power_task = task
        return {"scheduled": action, "delay_s": POWER_DELAY_S}

    def _schedule_restart_node(self) -> dict[str, Any]:
        assert self._restart_node is not None
        log.warning("Запрошен само-рестарт ноды через %.0f с", POWER_DELAY_S)

        async def run() -> None:
            await asyncio.sleep(POWER_DELAY_S)
            self._restart_node()

        task = asyncio.create_task(run(), name="restart-node")
        self._power_task = task
        return {"scheduled": ACTION_RESTART_NODE, "delay_s": POWER_DELAY_S}

    @staticmethod
    async def _blocking_sessions() -> tuple[list[ssh_sessions.SshSession], list[str]]:
        ssh = await ssh_sessions.list_ssh_sessions()
        tmux = await ssh_sessions.list_tmux_sessions()
        return ssh, tmux

    async def _close_ssh_sessions(self) -> dict[str, Any]:
        ssh, tmux = await self._blocking_sessions()
        await ssh_sessions.terminate_sessions([s.id for s in ssh])
        if tmux:
            await ssh_sessions.kill_tmux_server()
        result: dict[str, Any] = {"closed": len(ssh), "tmux_killed": len(tmux)}
        if ACTION_POWEROFF in self._power:
            result.update(self._schedule_power(ACTION_POWEROFF))
        return result

    async def maybe_auto_poweroff_idle(self) -> None:
        if not self._idle_poweroff:
            return
        ssh, tmux = await self._blocking_sessions()
        if not ssh and not tmux:
            self._schedule_power(ACTION_POWEROFF)
            return
        descriptions = [s.describe() for s in ssh] + [f"tmux: {name}" for name in tmux]
        log.warning(
            "%s: простой Alfred, но выключение отложено — %s",
            self._node,
            ", ".join(descriptions),
        )
        if self._emit is not None:
            await self._emit(
                EVENT_IDLE_POWER_BLOCKED,
                {"node": self._node, "sessions": descriptions},
            )
