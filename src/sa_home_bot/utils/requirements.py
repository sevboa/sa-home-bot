"""Внешние зависимости умений (smartctl, journalctl и т.п.) и диагностика.

Первую установку ноды (питон, git, сама программа) админ делает вручную —
это нормально и не автоматизируется здесь. А вот отдельные умения внутри уже
развёрнутой ноды не должны падать/шуметь в логи при нехватке внешней
программы, неподходящей ОС или недостатке прав: `Requirement` различает эти
три причины и даёт готовую подсказку, что сделать — под менеджер пакетов,
реально найденный в PATH текущей машины, либо (для нехватки прав) отсылку к
`nodectl fix` (см. `node/fixups.py`).

Нехватку прав нельзя определить статически (программа есть в PATH, но
реальный вызов упирается в permission denied) — это узнаётся только по факту
неудачного вызова подпроцесса, глубоко внутри `sensors/*.py`. Оттуда диагноз
репортится в модульный `requirements_registry`, а наружу (`get_state` службы
monitor) его читает `monitor/service.py`.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

# Первый найденный в PATH менеджер пакетов определяет команду подсказки.
_PACKAGE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("apt", "sudo apt install {pkg}"),
    ("pacman", "sudo pacman -S {pkg}"),
    ("dnf", "sudo dnf install {pkg}"),
    ("zypper", "sudo zypper install {pkg}"),
    ("apk", "sudo apk add {pkg}"),
    ("brew", "brew install {pkg}"),
)

# То же самое, но как argv для реального вызова (nodectl fix) — без "sudo":
# fixups сами решают, как звать sudo (интерактивно, наследуя TTY).
_INSTALL_ARGV: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apt", ("apt-get", "install", "-y")),
    ("pacman", ("pacman", "-S", "--noconfirm")),
    ("dnf", ("dnf", "install", "-y")),
    ("zypper", ("zypper", "--non-interactive", "install")),
    ("apk", ("apk", "add")),
    ("brew", ("brew", "install")),
)

# Маркеры отказа по правам в stderr внешних утилит (регистронезависимо).
_PERMISSION_MARKERS = (
    "permission denied",
    "operation not permitted",
    "must be root",
    "requires root",
    "eacces",
    "access is denied",  # Windows
)


def looks_like_permission_error(stderr: str) -> bool:
    """Похож ли вывод неудачного вызова на отказ по правам (не «нет программы»)."""
    low = stderr.lower()
    return any(marker in low for marker in _PERMISSION_MARKERS)


class RequirementStatus(StrEnum):
    """Диагноз `Requirement`: почему умение недоступно (если недоступно)."""

    OK = "ok"
    MISSING_PROGRAM = "missing_program"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    NEEDS_PRIVILEGE = "needs_privilege"


def _install_command(package: str) -> str:
    for manager, template in _PACKAGE_MANAGERS:
        if shutil.which(manager) is not None:
            return template.format(pkg=package)
    return f"установите пакет {package!r} через пакетный менеджер системы"


def install_argv(package: str) -> list[str] | None:
    """argv реальной команды установки под пакетный менеджер, найденный в PATH.

    None — ни один известный менеджер не найден (nodectl fix пропустит фикс с
    понятным предупреждением вместо падения).
    """
    for manager, argv in _INSTALL_ARGV:
        if shutil.which(manager) is not None:
            return [*argv, package]
    return None


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

    def diagnose(self) -> RequirementStatus:
        """Статический диагноз (без реального вызова): платформа/наличие в PATH.

        Не видит нехватку прав — та узнаётся только по факту неудачного
        вызова и репортится отдельно через `requirements_registry`.
        """
        if self.platforms is not None and sys.platform not in self.platforms:
            return RequirementStatus.UNSUPPORTED_PLATFORM
        if self.program is not None and shutil.which(self.program) is None:
            return RequirementStatus.MISSING_PROGRAM
        return RequirementStatus.OK

    def available(self) -> bool:
        return self.diagnose() is RequirementStatus.OK

    def install_hint(self) -> str:
        """Готовая подсказка админу — что сделать, чтобы умение заработало."""
        if self.platforms is not None and sys.platform not in self.platforms:
            where = " / ".join(self.platforms)
            base = f"не поддерживается на этой ОС (пока есть только для {where})"
        else:
            base = f"{_install_command(self.package)} (нужна команда {self.program!r})"
        return f"{base} — {self.note}" if self.note else base

    def privilege_hint(self) -> str:
        """Подсказка, когда программа есть, но не хватает прав на её вызов."""
        base = f"не хватает прав на команду {self.program!r} — выполните на ноде `nodectl fix`"
        return f"{base} — {self.note}" if self.note else base


@dataclass
class _RegistryEntry:
    requirement: Requirement
    status: RequirementStatus


class RequirementRegistry:
    """Живой реестр диагнозов, узнаваемых по факту реальных вызовов.

    Статическую часть (`OK`/`MISSING_PROGRAM`/`UNSUPPORTED_PLATFORM`) можно
    посчитать в любой момент через `Requirement.diagnose()`; `NEEDS_PRIVILEGE`
    узнаётся только тогда, когда код вызова подпроцесса реально попытался и
    поймал permission-denied — это и репортится сюда через `report()`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}
        self._lock = Lock()

    def report(self, requirement: Requirement, status: RequirementStatus) -> None:
        key = requirement.program or requirement.note or repr(requirement)
        with self._lock:
            self._entries[key] = _RegistryEntry(requirement, status)

    def status_for(self, requirement: Requirement) -> RequirementStatus:
        """Актуальный статус: живой статический диагноз, дополненный реестром.

        Статика (``diagnose()``) всегда пересчитывается заново — она дешёвая
        и не может протухнуть. Реестр даёт только то, что статикой не видно
        (``NEEDS_PRIVILEGE``), и то лишь пока статика говорит "программа на
        месте" — иначе реестр мог бы хранить протухший диагноз для уже
        удалённой/переустановленной программы.
        """
        static = requirement.diagnose()
        if static is not RequirementStatus.OK:
            return static
        key = requirement.program or requirement.note or repr(requirement)
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and entry.status is RequirementStatus.NEEDS_PRIVILEGE:
            return RequirementStatus.NEEDS_PRIVILEGE
        return RequirementStatus.OK

    def problem_for(self, requirement: Requirement) -> dict | None:
        """``{id,status,hint}`` для проблемного требования, иначе ``None``."""
        status = self.status_for(requirement)
        if status is RequirementStatus.OK:
            return None
        hint = (
            requirement.privilege_hint()
            if status is RequirementStatus.NEEDS_PRIVILEGE
            else requirement.install_hint()
        )
        return {"id": requirement.program or requirement.note, "status": status.value, "hint": hint}

    def reset(self) -> None:
        """Очистить реестр — используется в тестах для изоляции синглтона."""
        with self._lock:
            self._entries.clear()


requirements_registry = RequirementRegistry()
