"""Единый реестр команд: имена + описания. Источник правды для /help и меню."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    universal: bool  # True — работает везде без проверок


# Универсальные — всегда и везде, не указываются в allowed_commands.
HELP = Command("help", "список доступных команд", universal=True)
PING = Command("ping", "проверка живости (pong)", universal=True)
WHOAMI = Command("whoami", "показать user_id и chat_id", universal=True)

# Управляющие — требуют права в allowed_commands не-broken подписки.
STATUS = Command("status", "состояние компонентов (CPU/диски)", universal=False)
STATS = Command("stats", "статистика прогонов сканера", universal=False)
SCAN_NOW = Command("scan_now", "форс-скан датчиков", universal=False)

ALL_COMMANDS: list[Command] = [HELP, PING, WHOAMI, STATUS, STATS, SCAN_NOW]

UNIVERSAL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if c.universal]
CONTROL_COMMANDS: list[Command] = [c for c in ALL_COMMANDS if not c.universal]

_BY_NAME = {c.name: c for c in ALL_COMMANDS}


def get(name: str) -> Command | None:
    return _BY_NAME.get(name)


def is_universal(name: str) -> bool:
    cmd = _BY_NAME.get(name)
    return cmd is not None and cmd.universal


def is_control(name: str) -> bool:
    cmd = _BY_NAME.get(name)
    return cmd is not None and not cmd.universal
