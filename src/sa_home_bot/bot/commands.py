from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sa_home_bot.bot.ui.callback_data import pack

if TYPE_CHECKING:
    from sa_home_bot.subscriptions.models import Subscription


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    universal: bool
    menu: bool = True
    right: str | None = None


PING = Command("ping", "проверка живости (pong)", universal=True, menu=False)

SWARM = Command("swarm", "сводка роя", universal=False, menu=False, right="nodes")
NODES = Command("nodes", "сводка роя (алиас /swarm)", universal=False, menu=False)
STATUS = Command("status", "карточка локальной ноды", universal=False, menu=False)
STATUS_FULL = Command(
    "status_full", "подробный статус компонентов", universal=False, menu=False
)
STATS = Command("stats", "статистика прогонов сканера", universal=False, menu=False)
SCAN_NOW = Command("scan_now", "форс-скан датчиков и дисков", universal=False, menu=False)
DOWNTIME = Command(
    "downtime", "последние отключения машины", universal=False, menu=False
)
WAKE = Command("wake", "разбудить домашний ПК (Wake-on-LAN)", universal=False, menu=False)
ALFRED = Command("alfred", "позвать Альфреда", universal=False, menu=True, right="chat@llm")
AI = Command("ai", "то же самое, что /alfred", universal=False, menu=False, right="chat@llm")
INVITE = Command("invite", "выпустить код приглашения", universal=False, menu=False,
                 right="invite")
GUESTS = Command("guests", "приглашённые и открытые коды", universal=False, menu=False,
                 right="invite")
VPN = Command("vpn", "доступ к VPN", universal=False, menu=True, right="usage@vpn")
TORRENTS = Command(
    "torrents",
    "закачки: список, пауза/старт, лимит скорости",
    universal=False,
    menu=True,
    right="list@torrents",
)
INTERACTIVES = Command(
    "interactives", "сценки в этом чате: on/off", universal=True, menu=False
)
ITEMS = Command("items", "особенные вещи поместья", universal=True, menu=True)
DRAW = Command("draw", "отладка генерации картинок", universal=False, menu=False)
AWAY = Command(
    "away", "Альфред в городе: окно обслуживания", universal=False, menu=False, right="away"
)
BACK = Command("back", "вернуть Альфреда из города", universal=False, menu=False, right="away")
ALL_COMMANDS: list[Command] = [
    PING,
    INTERACTIVES,
    ITEMS,
    SWARM,
    NODES,
    STATUS,
    STATUS_FULL,
    STATS,
    SCAN_NOW,
    DOWNTIME,
    WAKE,
    ALFRED,
    AI,
    INVITE,
    GUESTS,
    VPN,
    TORRENTS,
    DRAW,
    AWAY,
    BACK,
]

UNIVERSAL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if c.universal]
CONTROL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if not c.universal]
MENU_UNIVERSAL_COMMANDS: list[Command] = [c for c in UNIVERSAL_COMMANDS if c.menu]
MENU_CONTROL_COMMANDS: list[Command] = [c for c in CONTROL_COMMANDS if c.menu]


def can_run_action(subscription: Subscription | None, action_id: str, service: str) -> bool:
    return subscription is not None and subscription.allows_action(action_id, service)


def menu_control_commands_for(subscription: Subscription | None) -> list[Command]:
    if subscription is None:
        return []
    return [c for c in MENU_CONTROL_COMMANDS if subscription.allows_command(c.right or c.name)]

STATUS_ACTIONS: dict[str, Command] = {
    "full": STATUS_FULL,
    "stats": STATS,
    "downtime": DOWNTIME,
}
CALLBACK_PREFIX = "st"

NODES_CODE = "nodes"
NODE_CARD_CODE = "nodecard"
SERVICE_CARD_CODE = "svc"
WAKE_CODE = "wake"

ACTION_CALLBACK_PREFIX = "act"


def panel_callback(code: str, *parts: object) -> str:
    return pack(CALLBACK_PREFIX, code, *parts)


def action_callback(
    action_id: str,
    value: str | None = None,
    node_id: str | None = None,
    service: str = "node",
) -> str:
    parts = [ACTION_CALLBACK_PREFIX, service, action_id]
    if value or node_id:
        parts.append(value or "")
    if node_id:
        parts.append(node_id)
    return ":".join(parts)


def wake_callback(node_id: str | None = None) -> str:
    parts = [CALLBACK_PREFIX, WAKE_CODE]
    if node_id:
        parts.append(node_id)
    return ":".join(parts)


def parse_wake_callback(data: str | None) -> str | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX or parts[1] != WAKE_CODE:
        return None
    return parts[2] if len(parts) > 2 and parts[2] else None


def parse_action_callback(data: str | None) -> tuple[str, str, str | None, str | None] | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != ACTION_CALLBACK_PREFIX or not parts[1] or not parts[2]:
        return None
    value = parts[3] if len(parts) > 3 and parts[3] else None
    node_id = parts[4] if len(parts) > 4 and parts[4] else None
    return parts[1], parts[2], value, node_id

DOWNTIME_PAGE_CODE = "downtime_page"

GUESTS_LIST_CODE = "g_list"
OPEN_CODES_LIST_CODE = "g_codes"
GUEST_CARD_CODE = "g_card"
GUEST_KICK_CONFIRM_CODE = "g_kick_ask"
GUEST_REVOKE_CODE = "g_kick"
CODE_REVOKE_CODE = "g_code_off"
GUEST_STATS_CODE = "g_stats"
GUEST_PERMS_CODE = "g_perms"
GUEST_PERM_OFF_CODE = "g_poff"
GUEST_PERM_ADD_LIST_CODE = "g_padd"
GUEST_PERM_ADD_CODE = "g_pon"
GUEST_GROUP_ADD_CODE = "g_gon"
GUEST_GROUP_OFF_CODE = "g_goff"

SWARM_PANEL_CODE = "sw_panel"
SWARM_SKILLS_CODE = "sw_skills"
SWARM_SKILL_CODE = "sw_skill"
SWARM_NODES_CODE = "sw_nodes"
SWARM_NODE_CODE = "sw_node"
SWARM_SVCS_CODE = "sw_svcs"
SWARM_SVC_CODE = "sw_svc"
SWARM_UPDATE_CODE = "sw_update"

TORRENTS_LIST_CODE = "t_list"
TORRENT_CARD_CODE = "t_card"
TORRENT_TOGGLE_CODE = "t_toggle"
TORRENT_SPEED_CODE = "t_speed"
TORRENT_APP_CODE = "t_app"

_ALL_CALLBACK_ACTIONS: dict[str, Command] = {
    **STATUS_ACTIONS,
    DOWNTIME_PAGE_CODE: DOWNTIME,
    GUESTS_LIST_CODE: GUESTS,
    OPEN_CODES_LIST_CODE: GUESTS,
    GUEST_CARD_CODE: GUESTS,
    GUEST_KICK_CONFIRM_CODE: GUESTS,
    GUEST_REVOKE_CODE: GUESTS,
    CODE_REVOKE_CODE: GUESTS,
    GUEST_STATS_CODE: GUESTS,
    GUEST_PERMS_CODE: GUESTS,
    GUEST_PERM_OFF_CODE: GUESTS,
    GUEST_PERM_ADD_LIST_CODE: GUESTS,
    GUEST_PERM_ADD_CODE: GUESTS,
    GUEST_GROUP_ADD_CODE: GUESTS,
    GUEST_GROUP_OFF_CODE: GUESTS,
    NODES_CODE: NODES,
    NODE_CARD_CODE: STATUS,
    SERVICE_CARD_CODE: NODES,
    WAKE_CODE: WAKE,
    SWARM_PANEL_CODE: SWARM,
    SWARM_SKILLS_CODE: SWARM,
    SWARM_SKILL_CODE: SWARM,
    SWARM_NODES_CODE: SWARM,
    SWARM_NODE_CODE: STATUS,
    SWARM_SVCS_CODE: NODES,
    SWARM_SVC_CODE: NODES,
    SWARM_UPDATE_CODE: SWARM,
    TORRENTS_LIST_CODE: TORRENTS,
    TORRENT_CARD_CODE: TORRENTS,
    TORRENT_TOGGLE_CODE: TORRENTS,
    TORRENT_SPEED_CODE: TORRENTS,
    TORRENT_APP_CODE: TORRENTS,
}

_BY_NAME = {c.name: c for c in ALL_COMMANDS}


def get(name: str) -> Command | None:
    return _BY_NAME.get(name)


def required_right(name: str) -> str:
    cmd = _BY_NAME.get(name)
    if cmd is None:
        return name
    return cmd.right or cmd.name


def is_universal(name: str) -> bool:
    cmd = _BY_NAME.get(name)
    return cmd is not None and cmd.universal


def is_control(name: str) -> bool:
    cmd = _BY_NAME.get(name)
    return cmd is not None and not cmd.universal


def command_for_callback(data: str | None) -> Command | None:
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return _ALL_CALLBACK_ACTIONS.get(parts[1])
