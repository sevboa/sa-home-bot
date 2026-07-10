"""Единый реестр команд: имена + описания. Источник правды для /help и меню."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    universal: bool  # True — работает везде без проверок
    menu: bool = True  # показывать в меню бота и /help


# Универсальные — всегда и везде, не указываются в allowed_commands.
HELP = Command("help", "список доступных команд", universal=True)
PING = Command("ping", "проверка живости (pong)", universal=True)
WHOAMI = Command("whoami", "показать user_id и chat_id", universal=True)

# Управляющие — требуют права в allowed_commands не-broken подписки.
# Меню бота — скилы роя первого уровня: динамические команды-приложения из
# describe службы apps (см. setup.build_menu_commands) + «Управление нодами»
# (/nodes). Остальное скрыто и вызывается кнопками: /status — карточка
# локальной ноды, доступная из списка нод.
NODES = Command("nodes", "управление нодами роя", universal=False)
STATUS = Command("status", "карточка локальной ноды", universal=False, menu=False)
STATUS_FULL = Command(
    "status_full", "подробный статус компонентов", universal=False, menu=False
)
STATS = Command("stats", "статистика прогонов сканера", universal=False, menu=False)
SCAN_NOW = Command("scan_now", "форс-скан датчиков и дисков", universal=False, menu=False)
DOWNTIME = Command(
    "downtime", "последние отключения машины", universal=False, menu=False
)
# Wake — кнопка в разделе /nodes; командой тоже работает, но в меню не нужна.
WAKE = Command("wake", "разбудить домашний ПК (Wake-on-LAN)", universal=False, menu=False)

ALL_COMMANDS: list[Command] = [
    HELP,
    PING,
    WHOAMI,
    NODES,
    STATUS,
    STATUS_FULL,
    STATS,
    SCAN_NOW,
    DOWNTIME,
    WAKE,
]

UNIVERSAL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if c.universal]
CONTROL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if not c.universal]
# Управляющие, попадающие в меню/help (сейчас только STATUS).
MENU_CONTROL_COMMANDS: list[Command] = [c for c in CONTROL_COMMANDS if c.menu]

# Кнопки-представления под /status: callback-код → команда. Действия служб
# (скан и т.п.) сюда не входят — они строятся динамически из describe (см.
# ACTION_CALLBACK_PREFIX).
STATUS_ACTIONS: dict[str, Command] = {
    "full": STATUS_FULL,
    "stats": STATS,
    "downtime": DOWNTIME,
}
CALLBACK_PREFIX = "st"

# Иерархия раздела нод: список нод («st:nodes») → карточка ноды
# («st:nodecard», = /status: мониторинг + службы) → карточка службы
# («st:svc:<имя>», данные + кнопки управления). Wake-on-LAN — «st:wake».
NODES_CODE = "nodes"
NODE_CARD_CODE = "nodecard"
SERVICE_CARD_CODE = "svc"
WAKE_CODE = "wake"

# Динамические действия служб: «act:<служба>:<действие>[:<значение>]»,
# например «act:monitor:scan_now» или «act:node:restart:telegram-bot».
# Право — `действие@служба` (Subscription.allows_action).
ACTION_CALLBACK_PREFIX = "act"


def parse_action_callback(data: str | None) -> tuple[str, str, str | None] | None:
    """Разобрать «act:<служба>:<действие>[:<значение>]» → (служба, действие, значение)."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != ACTION_CALLBACK_PREFIX or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2], (parts[3] if len(parts) > 3 and parts[3] else None)

# Пагинация /downtime («st:downtime_page:<offset>») — не кнопка под /status,
# но требует тех же прав, что и сама команда DOWNTIME.
DOWNTIME_PAGE_CODE = "downtime_page"
_ALL_CALLBACK_ACTIONS: dict[str, Command] = {
    **STATUS_ACTIONS,
    DOWNTIME_PAGE_CODE: DOWNTIME,
    NODES_CODE: NODES,
    NODE_CARD_CODE: STATUS,  # карточка ноды = данные /status
    SERVICE_CARD_CODE: NODES,  # карточка службы — часть управления нодами
    WAKE_CODE: WAKE,
}

_BY_NAME = {c.name: c for c in ALL_COMMANDS}


def get(name: str) -> Command | None:
    return _BY_NAME.get(name)


def is_universal(name: str) -> bool:
    cmd = _BY_NAME.get(name)
    return cmd is not None and cmd.universal


def is_control(name: str) -> bool:
    cmd = _BY_NAME.get(name)
    return cmd is not None and not cmd.universal


def command_for_callback(data: str | None) -> Command | None:
    """Разобрать callback_data «st:<код>[:<аргумент>]» в команду-действие.

    Третий сегмент (offset пагинации /downtime) игнорируется — код действия
    всегда второй сегмент.
    """
    if not data:
        return None
    parts = data.split(":")
    if len(parts) < 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return _ALL_CALLBACK_ACTIONS.get(parts[1])
