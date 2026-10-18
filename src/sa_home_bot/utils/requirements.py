from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

_PACKAGE_MANAGERS: tuple[tuple[str, str], ...] = (
    ("apt", "sudo apt install {pkg}"),
    ("pacman", "sudo pacman -S {pkg}"),
    ("dnf", "sudo dnf install {pkg}"),
    ("zypper", "sudo zypper install {pkg}"),
    ("apk", "sudo apk add {pkg}"),
    ("brew", "brew install {pkg}"),
    ("winget", "winget install {pkg}"),
)

_INSTALL_ARGV: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apt", ("apt-get", "install", "-y")),
    ("pacman", ("pacman", "-S", "--noconfirm")),
    ("dnf", ("dnf", "install", "-y")),
    ("zypper", ("zypper", "--non-interactive", "install")),
    ("apk", ("apk", "add")),
    ("brew", ("brew", "install")),
)

_PERMISSION_MARKERS = (
    "permission denied",
    "operation not permitted",
    "must be root",
    "requires root",
    "eacces",
    "access is denied",
)


def looks_like_permission_error(stderr: str) -> bool:
    low = stderr.lower()
    return any(marker in low for marker in _PERMISSION_MARKERS)


SUDO_PASSWORD_MARKER = "a password is required"
SUDO_MESSAGE_PREFIX = "sudo:"


def looks_like_sudo_denied(stderr: str) -> bool:
    return looks_like_permission_error(stderr) or SUDO_PASSWORD_MARKER in stderr.lower()


def looks_like_sudo_message(stderr: str) -> bool:
    return (
        looks_like_permission_error(stderr)
        or stderr.strip().lower().startswith(SUDO_MESSAGE_PREFIX)
    )


class RequirementStatus(StrEnum):
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
    for manager, argv in _INSTALL_ARGV:
        if shutil.which(manager) is not None:
            return [*argv, package]
    return None


@dataclass(frozen=True)
class Requirement:
    program: str | None = None
    package: str = ""
    platforms: tuple[str, ...] | None = None
    note: str = ""
    key: str | None = None

    def diagnose(self) -> RequirementStatus:
        if self.platforms is not None and sys.platform not in self.platforms:
            return RequirementStatus.UNSUPPORTED_PLATFORM
        if self.program is not None and shutil.which(self.program) is None:
            return RequirementStatus.MISSING_PROGRAM
        return RequirementStatus.OK

    def available(self) -> bool:
        return self.diagnose() is RequirementStatus.OK

    def install_hint(self) -> str:
        if self.platforms is not None and sys.platform not in self.platforms:
            where = " / ".join(self.platforms)
            base = f"не поддерживается на этой ОС (пока есть только для {where})"
        else:
            base = f"{_install_command(self.package)} (нужна команда {self.program!r})"
        return f"{base} — {self.note}" if self.note else base

    def privilege_hint(self) -> str:
        base = f"не хватает прав на команду {self.program!r} — выполните на ноде `nodectl fix`"
        return f"{base} — {self.note}" if self.note else base


@dataclass
class _RegistryEntry:
    requirement: Requirement
    status: RequirementStatus


class RequirementRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _RegistryEntry] = {}
        self._lock = Lock()

    @staticmethod
    def _key(requirement: Requirement) -> str:
        return (
            requirement.key
            or requirement.program
            or requirement.note
            or repr(requirement)
        )

    def report(self, requirement: Requirement, status: RequirementStatus) -> None:
        with self._lock:
            self._entries[self._key(requirement)] = _RegistryEntry(requirement, status)

    def status_for(self, requirement: Requirement) -> RequirementStatus:
        static = requirement.diagnose()
        if static is not RequirementStatus.OK:
            return static
        with self._lock:
            entry = self._entries.get(self._key(requirement))
        if entry is not None and entry.status is RequirementStatus.NEEDS_PRIVILEGE:
            return RequirementStatus.NEEDS_PRIVILEGE
        return RequirementStatus.OK

    def problem_for(self, requirement: Requirement) -> dict | None:
        status = self.status_for(requirement)
        if status is RequirementStatus.OK:
            return None
        hint = (
            requirement.privilege_hint()
            if status is RequirementStatus.NEEDS_PRIVILEGE
            else requirement.install_hint()
        )
        return {
            "id": requirement.key or requirement.program or requirement.note,
            "status": status.value,
            "hint": hint,
        }

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


requirements_registry = RequirementRegistry()
