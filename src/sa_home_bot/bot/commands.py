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
# STATUS — единственная в меню; остальные скрыты и вызываются кнопками под /status.
STATUS = Command("status", "краткая сводка состояния", universal=False)
STATUS_FULL = Command(
    "status_full", "подробный статус компонентов", universal=False, menu=False
)
STATS = Command("stats", "статистика прогонов сканера", universal=False, menu=False)
SCAN_NOW = Command("scan_now", "форс-скан датчиков и дисков", universal=False, menu=False)
DOWNTIME = Command(
    "downtime", "последние отключения машины", universal=False, menu=False
)

ALL_COMMANDS: list[Command] = [
    HELP,
    PING,
    WHOAMI,
    STATUS,
    STATUS_FULL,
    STATS,
    SCAN_NOW,
    DOWNTIME,
]

UNIVERSAL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if c.universal]
CONTROL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if not c.universal]
# Управляющие, попадающие в меню/help (сейчас только STATUS).
MENU_CONTROL_COMMANDS: list[Command] = [c for c in CONTROL_COMMANDS if c.menu]

# Кнопки-действия под /status: callback-код → команда.
STATUS_ACTIONS: dict[str, Command] = {
    "full": STATUS_FULL,
    "stats": STATS,
    "downtime": DOWNTIME,
    "scan": SCAN_NOW,
}
CALLBACK_PREFIX = "st"

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
    """Разобрать callback_data кнопки «st:<код>» в команду-действие."""
    if not data:
        return None
    parts = data.split(":", 1)
    if len(parts) != 2 or parts[0] != CALLBACK_PREFIX:
        return None
    return STATUS_ACTIONS.get(parts[1])
