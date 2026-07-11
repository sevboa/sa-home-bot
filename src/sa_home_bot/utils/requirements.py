"""Внешние зависимости умений (smartctl, будущие Windows-специфичные и т.п.).

Первую установку ноды (питон, git, сама программа) админ делает вручную —
это нормально и не автоматизируется здесь. А вот отдельные умения внутри уже
развёрнутой ноды не должны падать/шуметь в логи при нехватке внешней
программы или неподходящей ОС: `Requirement` проверяет себя раз за вызов
и даёт готовую подсказку, что установить и какой командой — под менеджер
пакетов, реально найденный в PATH текущей машины.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass

# Первый найденный в PATH менеджер пакетов определяет команду подсказки.
_PACKAGE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("apt", "sudo apt install {pkg}"),
    ("pacman", "sudo pacman -S {pkg}"),
    ("dnf", "sudo dnf install {pkg}"),
    ("zypper", "sudo zypper install {pkg}"),
    ("apk", "sudo apk add {pkg}"),
    ("brew", "brew install {pkg}"),
)


def _install_command(package: str) -> str:
    for manager, template in _PACKAGE_MANAGERS:
        if shutil.which(manager) is not None:
            return template.format(pkg=package)
    return f"установите пакет {package!r} через пакетный менеджер системы"


@dataclass(frozen=True)
class Requirement:
    """Зависимость умения: программа в PATH и/или ограничение по ОС.

    ``program``/``package`` — нужна внешняя программа (package — чем её
    ставить). ``platforms`` — умение работает только на этих ``sys.platform``
    (напр. ``("win32",)``) — тогда на прочих ОС не «нет программы», а «пока
    не реализовано». Можно задать оба поля сразу.
    """

    program: str | None = None
    package: str = ""
    platforms: tuple[str, ...] | None = None
    note: str = ""

    def available(self) -> bool:
        if self.platforms is not None and sys.platform not in self.platforms:
            return False
        if self.program is not None and shutil.which(self.program) is None:
            return False
        return True

    def install_hint(self) -> str:
        """Готовая подсказка админу — что сделать, чтобы умение заработало."""
        if self.platforms is not None and sys.platform not in self.platforms:
            where = " / ".join(self.platforms)
            base = f"не поддерживается на этой ОС (пока есть только для {where})"
        else:
            base = f"{_install_command(self.package)} (нужна команда {self.program!r})"
        return f"{base} — {self.note}" if self.note else base
