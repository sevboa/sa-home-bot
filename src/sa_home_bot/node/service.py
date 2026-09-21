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
from collections.abc import Callable, Sequence
from typing import Any

from sa_home_bot import __version__, wol
from sa_home_bot.config import SwarmNodeConfig
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

ACTION_SWARM_JOIN = "swarm_join"  # входящее: сосед присоединяется к нам
ACTION_JOIN = "join"  # исходящее: мы присоединяемся к соседу (nodectl join)
EVENT_NODE_JOINED = "node_joined"

ACTION_RESTART_NODE = "restart_node"

ACTION_POWEROFF = "poweroff"
ACTION_REBOOT = "reboot"
ACTION_SUSPEND = "suspend"

# Автовыключение по простою Alfred (config.py::NodeConfig.idle_poweroff,
# maybe_auto_poweroff_idle ниже): если открыта SSH-сессия ИЛИ живёт
# tmux-сессия (переживает разрыв SSH — utils/ssh_sessions.py), выключение
# откладывается и рою уходит это событие — админ либо сам разберётся и
# перевыключит руками, либо нажмёт кнопку ACTION_CLOSE_SSH_SESSIONS.
ACTION_CLOSE_SSH_SESSIONS = "close_ssh_sessions"
CLOSE_SSH_SESSIONS_TITLE = "🔌 Закрыть SSH+tmux и выключить"
EVENT_IDLE_POWER_BLOCKED = "idle_power_blocked"

ACTION_CHECK_UPDATE = "check_update"
ACTION_UPDATE = "update"
EVENT_UPDATE_FINISHED = "update_finished"

# Эмитится один раз при старте процесса, если версия успела смениться с
# прошлого известного старта (node/state.py::last_known_version) — то есть
# ФАЙЛЫ уже реально ИСПОЛНЯЮТСЯ, в отличие от update_finished (файлы легли
# на диск, но процесс мог и не перезапуститься). Решение пользователя
# 2026-08-04: без этого события отложенная задача-продолжение (remind
# after_event, bot/tools.py) не может отличить «update лёг на диск» от
# «рестарт реально применил новую версию» — а Альфред уже путал одно с
# другим, докладывая «готово» до фактического restart_node. См.
# node/app.py::_announce_version_if_changed — сравнение и эмит живут там
# (там же лежит __version__ и state), не здесь.
EVENT_RESTART_APPLIED = "restart_applied"

ACTION_SEND_WOL = "send_wol"  # рой просит ЭТУ ноду разбудить кого-то в её LAN

# Дочерний процесс слота-синглтона (сейчас — только telegram-bot) сообщает:
# я реально поднялся, а не просто спавнился ОС-процессом (для бота — успешный
# bot.get_me(), см. app.py::run). Служебное действие, зовёт только сам
# процесс по уже открытому node_link — человеку в UI делать тут нечего,
# поэтому без choices на параметрах (см. describe()). Уходит в
# LeaseManager.set_ready()/local_state()["ready"] — на нём строит решение
# LeaseManager._decide() соседей (node/lease.py, SUPERIOR_READY_TIMEOUT_S).
ACTION_REPORT_READY = "report_ready"

# Generic fan-out: разослать команду именованной службе на себе + всех
# живых пирах, где она поднята (сейчас единственный такой примитив в рое —
# остальные действия строго "один запрос — один узел"). Не ждёт результата
# самой работы дольше timeout_s — только подтверждение доставки; вызываемая
# служба либо отвечает мгновенно (ack), либо сама асинхронно шлёт результат
# отдельным вызовом инициатору (см. vpn_check → vpn/report_check).
# Служебное действие (как report_ready) — без choices, не для UI.
ACTION_TRIGGER_PEERS = "trigger_peers"

RESTART_NODE_TITLE = "🔄 Перезапустить ноду"
SWARM_JOIN_TITLE = "🤝 Присоединить ноду"
JOIN_TITLE = "🔗 Присоединиться к рою"
CHECK_UPDATE_TITLE = "🔍 Проверить обновления"
UPDATE_TITLE = "⬆️ Обновить"
SEND_WOL_TITLE = "📡 Отправить Wake-on-LAN"

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


def _endpoint_list(raw: Any, *, first: str) -> list[str]:
    """Список адресов из сообщения роя, с ``first`` во главе.

    Поле ``endpoints`` появилось в этапе 24 и необязательно: нода старой
    версии присылает только ``endpoint``, и тогда список из него одного —
    ровно прежнее поведение.
    """
    values = [first] if first else []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item and item not in values:
                values.append(item)
    return values


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
        # Репликация пакетов настроек: None — на этой ноде каталога пакетов
        # нет (конфиг не файловый), обмениваться нечем.
        self._replicator = replicator
        # Аренда синглтонов: None — в тестах/сборках без неё.
        self._lease = lease
        self._supervisor = supervisor
        self._router = router
        self._node = node_id or socket.gethostname()
        # Тип машины: не привилегия, а ответ на «ждать ли ноду всегда», «можно
        # ли будить», «есть ли датчики железа» — см. node/kind.py.
        self._kind = node_kind
        self._traits = node_kinds.traits_for(node_kind)
        self._runtime = Runtime()
        # Power-действия (poweroff/reboot/suspend) объявляются только машине,
        # которую можно добровольно уводить в офлайн — см. NodeTraits.power_controllable.
        # ``power_controllable`` (параметр конструктора, из [node].power_controllable
        # конфига) — явный оверрайд для конкретной машины: always_on-нода всё
        # равно алертит о пропаже (traits.always_on не трогаем), но физически
        # доступный домашний сервер можно разрешить выключать/перезагружать
        # кнопкой, в отличие от удалённого VDS без такого оверрайда.
        controllable = (
            self._traits.power_controllable
            if power_controllable is None
            else power_controllable
        )
        self._power = power_commands() if controllable else {}
        self._power_runner = power_runner or _default_power_runner
        # Автовыключение по простою Alfred (см. config.py::NodeConfig.idle_poweroff)
        # — осмысленно, только если машину вообще можно выключить; иначе
        # флаг молча не действует, а не падает конфигом на server/vps.
        self._idle_poweroff = idle_poweroff and controllable
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
        # Фабрики линков из node/app.py::make_link_factories: несут on_event и
        # self_node, которые здесь взять больше неоткуда. Живой баг (аудит
        # 2026-07-28): линки, собранные тут на месте голым PeerLink(...,
        # token=...), не принимали события соседа/службы до рестарта ноды и
        # не представлялись в auth. Запасной вариант — для тестов и сборок
        # без событийной части.
        self._make_peer_link = make_peer_link or (
            lambda pid, ep: PeerLink(pid, ep, token=swarm_token, self_node=self._node)
        )
        self._make_local_link = make_local_link or (
            lambda name, ep: PeerLink(
                name, ep, token=swarm_token, heartbeat_timeout=LOCAL_HEARTBEAT_TIMEOUT_S
            )
        )
        # swarm_join: без своего TCP-адреса нечего давать соседям для обратной
        # связи — действие объявляется только когда есть куда стучаться.
        self._own_endpoint = own_endpoint
        # Чем нода представляется рою (этап 24): все реально слушаемые адреса,
        # а не один из конфига. Живёт колбэком, потому что список меняется —
        # tailscale-адрес приезжает через десятки секунд после старта, LAN
        # доступен сразу (см. ProtoServer.advertised_endpoints).
        self._advertise = advertise
        self._emit = emit
        # Самообновление через pipx (не требует root — в отличие от
        # `nodectl fix`, можно звать прямо из этого процесса). None — ставили
        # не из git-репозитория (dev-чекаут и т.п.) — умение не объявляется.
        self._update_source = update_source
        self._updating = False
        self._last_update: dict[str, Any] | None = None
        # Маячок discovery (node/discovery.py) собирается ПОСЛЕ службы — ему
        # нужен наш же join(), — поэтому не параметр конструктора, а колбэк,
        # который node/app.py прицепляет следом. None — маячка нет.
        self._discovery_state: Callable[[], dict[str, Any]] | None = None

    def attach_discovery(self, state: Callable[[], dict[str, Any]]) -> None:
        """Показывать состояние маячка в get_state: без этого «почему сосед
        не нашёлся» выясняется только по логам."""
        self._discovery_state = state

    def _assignable_here(self) -> tuple[str, ...]:
        """Что осмысленно назначить именно этой машине.

        Служба, которой нужны термозоны и SMART, на виртуалке бессмысленна —
        не предлагаем её в choices, чтобы бот не рисовал кнопку, ведущую к
        службе, которой нечего мерить. Уже назначенное (правкой конфига,
        например) при этом работает — фильтр только про предложение.
        """
        names = registry.assignable_names()
        if self._traits.hardware_sensors:
            return names
        return tuple(
            n for n in names if not registry.SERVICES[n].needs_hardware_sensors
        )

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
            choices=self._assignable_here(),
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
                # Без choices — служебное действие, не для UI (см. константу).
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
                # Репликация настроек: оба действия с обязательными
                # параметрами, поэтому фронтенды их кнопками не рисуют и зовёт
                # их только другая нода (PROTOCOL.md, «команды-представления»).
                # get_instance_config отдаёт секреты — канал внутри роя уже
                # защищён (TCP + токен роя, unix-сокет — правами файла).
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
        """Свои адреса для рою — реально слушаемые, иначе настроенный.

        Запасной вариант нужен не только тестам: пока сервер не поднялся (и в
        сборках без него) единственная правда о себе — это конфиг.
        """
        live = self._advertise() if self._advertise is not None else []
        if live:
            return live
        return [self._own_endpoint] if self._own_endpoint else []

    @staticmethod
    def _local_wake_payload() -> dict[str, str] | None:
        """Свои Ethernet-реквизиты для WoL (None — Wi-Fi/без LAN). Один вид
        для hello (ServiceInfo.wake) и для get_state()["wake"] — соседи
        кладут в кэш то же самое, откуда бы ни узнали."""
        info = wol.detect_local_wake_info()
        if info is None:
            return None
        return {"mac": info.mac, "ip": info.ip, "broadcast": info.broadcast}

    def _services_state(self) -> list[dict[str, Any]]:
        """Службы ноды для /nodes и `nodectl status`.

        Супервизор знает не все: внешне управляемые назначения (llm на
        Windows-ноде — её поднимает scheduled task в интерактивной сессии,
        см. deploy/llm-runner.ps1) он сознательно не спавнит, поэтому в
        `supervisor.services` их нет. Живой баг 2026-07-28: из-за этого
        рабочая llm вообще не показывалась в рое — «на ноде winpc нет службы
        llm», хотя она отвечала. Берём их из маршрутизатора: линк к локальной
        службе есть ровно тогда, когда она назначена, а его `alive` — честное
        состояние (процесс отвечает), лучшее, что о ней вообще известно
        снаружи: ни pid, ни времени старта чужого процесса нода не знает.
        """
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
                    # Фронтенду: это не наш процесс — не предлагать start/stop,
                    # супервизор такую службу не поднимет (см. nodectl.py).
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
            # Ревизии пакетов настроек — по ним соседи понимают, у кого версия
            # свежее, и подтягивают её (node/replication.py).
            "instances": (
                self._replicator.local_revisions() if self._replicator is not None else []
            ),
            # Кто из нод держит службу-синглтон: на этом соседи строят своё
            # решение брать/уступать (node/lease.py).
            "singletons": self._lease.local_state() if self._lease is not None else {},
            "uptime_s": round(self._runtime.uptime_seconds(), 1),
            "system_uptime_s": system_uptime_seconds(),
            "services": self._services_state(),
            "peers": self._router.peers_state() if self._router is not None else [],
            # Свои Ethernet-реквизиты (None — Wi-Fi/без LAN): бот кэширует их,
            # пока нода жива, и использует, когда та уснёт (этап 19 п.6).
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
            assignment = assignments_mod.parse(name)
            svc = self._supervisor.assign(name)
        except ValueError as exc:
            raise ProtoError(ERR_BAD_REQUEST, str(exc)) from exc
        # Синглтон не запускаем прямо здесь: его черёд определит аренда
        # лидерства, убедившись, что службу не держит другая нода
        # (node/lease.py). Для активной роли это ожидание нулевое.
        spec = registry.spec(assignment.service)
        if spec is None or not spec.singleton:
            await svc.start()
        # Маршрут — по имени службы: "telegram-bot@alfred" и "telegram-bot"
        # ведут к одному и тому же сокету.
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
        # Снять можно и по ключу слота ("telegram-bot@alfred"), и по строке
        # назначения целиком — так же, как её задавали.
        try:
            key = assignments_mod.parse(name).key
        except assignments_mod.AssignmentError:
            key = name
        if self._supervisor.get(key) is None:
            known = ", ".join(self._supervisor.services) or "нет служб"
            raise ProtoError(ERR_BAD_REQUEST, f"нет такой службы: {name!r} (есть: {known})")
        service = self._supervisor.services[key].assignment.service
        await self._supervisor.unassign(key)
        if self._router is not None:
            await self._router.remove_local_service(service)
        # В состоянии лежит исходная строка назначения — найти её по ключу.
        for item in list(self._state.assignments):
            try:
                same = assignments_mod.parse(item).key == key
            except assignments_mod.AssignmentError:
                same = item == key
            if same:
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
        """Отдать пакет настроек соседу. Зовётся нодой, не человеком: пакет
        содержит секреты службы (токен бота), поэтому едет адресным ответом
        по защищённому каналу роя, а не broadcast'ом."""
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
        """Дочерний процесс слота-синглтона подтверждает (или снимает)
        готовность — см. ACTION_REPORT_READY. Best-effort со стороны
        вызывающего (app.py::run), поэтому здесь достаточно мягкой валидации:
        неизвестное имя слота — явная ошибка вызывающему, отсутствие аренды
        на этой сборке/в тестах — тихий no-op, а не падение."""
        name = str(args.get("name", ""))
        if self._supervisor.get(name) is None:
            known = ", ".join(self._supervisor.services) or "нет служб"
            raise ProtoError(ERR_BAD_REQUEST, f"нет такой службы: {name!r} (есть: {known})")
        ready = bool(args.get("ready", True))
        if self._lease is not None:
            self._lease.set_ready(name, ready)
        return {"name": name, "ready": ready}

    async def _trigger_peers(self, args: dict[str, Any]) -> dict[str, Any]:
        """Fan-out — см. ACTION_TRIGGER_PEERS. Пробует себя (по своему же
        node_id, роутер сам разрулит "локальная служба") + всех живых
        пиров; для каждого отдельно ловит "нет такой службы" (пропустить
        молча, не у всех нод она поднята) и "недоступна" (сеть/таймаут —
        не валит всю операцию). Возвращает только сводку доставки, не
        результат самой команды — тот, если нужен, вызываемая служба
        репортит отдельно инициатору сама."""
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
        """Разослать magic packet в СВОЙ LAN-сегмент — вызывается пиром роя,
        когда именно эта нода оказалась в одной подсети со спящей целью
        (bot/wake_state.py + bot/swarm_view.find_lan_waker, этап 19 п.6)."""
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
        """Принять присоединение соседа: узнать о нём, вернуть полный граф
        известных пиров (включая себя) за один round-trip — присоединяющийся
        сразу может связаться со всеми, без цепочки отдельных запросов.
        """
        caller_id = str(args.get("node_id", ""))
        caller_endpoint = str(args.get("endpoint", ""))
        # Список адресов присоединяющегося (этап 24) — необязательный: нода
        # старой версии пришлёт только endpoint, и это по-прежнему работает.
        caller_endpoints = _endpoint_list(args.get("endpoints"), first=caller_endpoint)
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
            self._remember_peer(caller_id, caller_endpoint, caller_endpoints)

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

    def _remember_peer(self, node_id: str, endpoint: str, endpoints: Sequence[str] = ()) -> None:
        """Персистентный справочник пиров (не полный конфиг соседа — только
        id + последний удачный адрес + прочие известные пути, node/state.py).

        Тип соседа при перезаписи сохраняется — см. node/app.py::_remember_peer.
        """
        known = next((p for p in self._state.peers if p.id == node_id), None)
        others = [p for p in self._state.peers if p.id != node_id]
        rest = [e for e in endpoints if e != endpoint]
        self._state.peers = [
            *others,
            SwarmNodeConfig(
                id=node_id,
                endpoint=endpoint,
                endpoints=rest,
                kind=known.kind if known is not None else "",
            ),
        ]
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
                peer_endpoints = _endpoint_list(peer.get("endpoints"), first=peer_endpoint)
                link = self._make_peer_link(pid, peer_endpoints)
                await self._router.add_peer(link)
                self._remember_peer(pid, peer_endpoint, peer_endpoints)
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
        """Подтянуть последний тег — БЕЗ рестарта процесса (на Linux) или
        через внешнюю задачу планировщика, которая рестарт делает сама
        (на win32, см. _schedule_windows_update).

        На Linux файлы на диске обновляются сразу; уже загруженный в память
        код продолжает работать по-старому, пока человек не выполнит
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
            # Живой баг 2026-08-04: up_to_date сравнивал ТОЛЬКО installed
            # (на диске) с target — если файлы уже легли раньше (этот же
            # update уже вызывали, restart_node ещё не делали), ответ
            # выглядел как "полностью готово", и Альфред пропускал
            # необходимый restart_node, доложив о готовности ноды, которая
            # на деле ещё исполняет старый код. running (== __version__)
            # добавлен явно, чтобы вызывающий (bot/tools.py::
            # tool_node_manage) не спутал "на диске" с "исполняется".
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
        """На win32 pipx_reinstall в процессе не работает (WinError 5 — см.
        update_source_for_this_platform) — вместо этого дёргаем задачу
        планировщика (deploy/win-auto-update.ps1): она сама стопает службу,
        ставит pipx, стартует заново. Процесс, ответивший на этот RPC, скоро
        сам умрёт от Stop-Service — событие об успехе эмитить уже некому,
        поэтому фиксируем только неудачу (задача не запустилась вообще)."""
        self._updating = True

        async def run() -> None:
            try:
                ok, output = await node_update.trigger_scheduled_task()
            except Exception as exc:  # noqa: BLE001 — фон не должен уронить ноду
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
        self._update_task = task  # ссылка, чтобы задачу не собрал GC
        return {"scheduled": True, "target_version": target_version, "via": "scheduled_task"}

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

    @staticmethod
    async def _blocking_sessions() -> tuple[list[ssh_sessions.SshSession], list[str]]:
        """Оба независимых сигнала «за машиной работают руками» — открытые
        SSH-сессии и живые tmux-сессии (та переживает разрыв SSH — см.
        докстроку utils/ssh_sessions.py, живая находка 2026-08-03: без этой
        проверки отсоединившийся tmux с фоновой задачей не остановил бы
        автовыключение)."""
        ssh = await ssh_sessions.list_ssh_sessions()
        tmux = await ssh_sessions.list_tmux_sessions()
        return ssh, tmux

    async def _close_ssh_sessions(self) -> dict[str, Any]:
        """Кнопка/действие «выгнать всех и выключить» — и ручное (карточка
        ноды), и то, что жмёт админ из уведомления `EVENT_IDLE_POWER_BLOCKED`.
        Одно действие, не два шага (решение пользователя 2026-08-03): раз уж
        закрываем чужие сессии, дальше выключаем сразу же, а не ждём
        следующего простоя Alfred. tmux гасится целиком (kill-server, вместе
        со всем, что в нём запущено) — тем же решением: кнопка означает
        «выключить», а не «просто отсоединить»."""
        ssh, tmux = await self._blocking_sessions()
        await ssh_sessions.terminate_sessions([s.id for s in ssh])
        if tmux:
            await ssh_sessions.kill_tmux_server()
        result: dict[str, Any] = {"closed": len(ssh), "tmux_killed": len(tmux)}
        if ACTION_POWEROFF in self._power:
            result.update(self._schedule_power(ACTION_POWEROFF))
        return result

    async def maybe_auto_poweroff_idle(self) -> None:
        """Вызывается node/app.py::on_local_event на КАЖДОЕ натуральное (не
        тихое — см. llm/service.py::_sleep_now) событие `llm_went_idle` этой
        же ноды — эмитится безусловно, даже если ни одного обращения к
        Alfred за это время не было (в отличие от адресованного чатам
        `llm_idle_sleep`). Ручной роспуск («Альфред, ты свободен») машину не
        трогает — это отдельное, явное решение человека, а не простой.

        Открытая SSH-сессия или живая tmux-сессия — вероятный признак того,
        что за машиной сейчас работают руками, не через Alfred
        (config.py::NodeConfig.idle_poweroff)."""
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
