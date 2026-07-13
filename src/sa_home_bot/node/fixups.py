"""Рецепты повышения привилегий, выполняемые вручную по SSH (`nodectl fix`).

Короткоживущий процесс, не демон: читает конфиг локально (как остальной
`nodectl`), определяет, какие фиксы нужны исходя из назначений ноды, и для
каждого непройденного `check()` зовёт настоящий интерактивный `sudo`
(наследует TTY — пароль нигде не хранится и никуда не передаётся по сети).
Долгоживущий процесс ноды (`node/service.py`) сам `sudo` не вызывает и прав
не хранит — этот инвариант fixups не нарушают.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path

from sa_home_bot.config import AppConfig, Settings
from sa_home_bot.sensors.disks import SMARTCTL_REQUIREMENT
from sa_home_bot.utils.requirements import install_argv

log = logging.getLogger(__name__)

SUDOERS_DIR = Path("/etc/sudoers.d")


class FixupError(Exception):
    """Фикс не удалось применить — `nodectl fix` продолжает со следующим."""


@dataclass(frozen=True)
class Fixup:
    id: str
    title: str
    needed: Callable[[Settings], bool]  # нужен ли фикс исходя из назначений ноды
    check: Callable[[], bool]  # уже применён? (идемпотентность)
    apply: Callable[[], None]  # выполнить (может звать интерактивный sudo)


def _sudo(argv: list[str]) -> None:
    """Настоящий интерактивный ``sudo`` — наследует TTY, пароль нигде не хранится."""
    result = subprocess.run(["sudo", *argv])
    if result.returncode != 0:
        raise FixupError(f"sudo {' '.join(argv)} завершился кодом {result.returncode}")


def _install_sudoers_snippet(name: str, content: str) -> None:
    """Валидировать содержимое через ``visudo`` и установить файл под sudo."""
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        check = subprocess.run(
            ["visudo", "-cf", str(tmp_path)], capture_output=True, text=True
        )
        if check.returncode != 0:
            raise FixupError(f"visudo отверг сниппет {name}: {check.stderr.strip()}")
        _sudo(
            [
                "install",
                "-m",
                "0440",
                "-o",
                "root",
                "-g",
                "root",
                str(tmp_path),
                str(SUDOERS_DIR / name),
            ]
        )
    finally:
        tmp_path.unlink(missing_ok=True)


# --- smartmontools: установка пакета ---


def _smartmontools_needed(settings: Settings) -> bool:
    return "monitor" in settings.node.assignments and settings.sensors.disks.enabled


def _smartmontools_check() -> bool:
    return shutil.which("smartctl") is not None


def _smartmontools_apply() -> None:
    argv = install_argv(SMARTCTL_REQUIREMENT.package)
    if argv is None:
        raise FixupError("не найден известный пакетный менеджер для smartmontools")
    _sudo(argv)


INSTALL_SMARTMONTOOLS = Fixup(
    id="install-smartmontools",
    title="Установить smartmontools",
    needed=_smartmontools_needed,
    check=_smartmontools_check,
    apply=_smartmontools_apply,
)


# --- smartctl: узкий sudoers-снипет (NOPASSWD только на конкретный бинарник) ---

SMARTCTL_SUDOERS_FILE = "50-sa-home-node-smartctl"


def _smartctl_sudoers_check() -> bool:
    return (SUDOERS_DIR / SMARTCTL_SUDOERS_FILE).exists()


def smartctl_sudoers_content(smartctl_path: str, user: str) -> str:
    """Содержимое sudoers-снипета: NOPASSWD только на резолвленный путь
    smartctl (не голое имя — защита от PATH-hijack), с любыми аргументами."""
    return f"{user} ALL=(root) NOPASSWD: {smartctl_path} *\n"


def _smartctl_sudoers_apply() -> None:
    path = shutil.which("smartctl")
    if path is None:
        raise FixupError("smartctl не найден в PATH — сначала install-smartmontools")
    _install_sudoers_snippet(SMARTCTL_SUDOERS_FILE, smartctl_sudoers_content(path, getuser()))


SMARTCTL_SUDOERS = Fixup(
    id="smartctl-sudoers",
    title="Разрешить smartctl без пароля (узкий sudoers)",
    needed=_smartmontools_needed,
    check=_smartctl_sudoers_check,
    apply=_smartctl_sudoers_apply,
)


# --- journalctl: доступ к журналу без root (группа systemd-journal) ---


def _journalctl_needed(settings: Settings) -> bool:
    return "monitor" in settings.node.assignments


def _in_group(group: str) -> bool:
    try:
        out = subprocess.run(
            ["id", "-nG", getuser()], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return group in out.stdout.split()


def _journalctl_group_check() -> bool:
    return _in_group("systemd-journal")


def _journalctl_group_apply() -> None:
    _sudo(["usermod", "-aG", "systemd-journal", getuser()])
    log.warning(
        "Группа systemd-journal добавлена — применится после нового логина/сессии, "
        "не мгновенно в текущей."
    )


JOURNALCTL_GROUP = Fixup(
    id="journalctl-group",
    title="Добавить пользователя в группу systemd-journal",
    needed=_journalctl_needed,
    check=_journalctl_group_check,
    apply=_journalctl_group_apply,
)


# --- apps: systemctl start/stop/restart без пароля, по одному снипету на юнит ---


def _apps_unit_sudoers_file(app_id: str) -> str:
    return f"50-sa-home-node-apps-{app_id}"


def _apps_unit_needed(settings: Settings) -> bool:
    return "apps" in settings.node.assignments


def apps_unit_sudoers_content(app: AppConfig, systemctl_path: str, user: str) -> str:
    """Содержимое sudoers-снипета: NOPASSWD только на start/stop/restart
    конкретного юнита — не произвольные systemctl-команды."""
    return (
        f"{user} ALL=(root) NOPASSWD: "
        f"{systemctl_path} start {app.unit}, "
        f"{systemctl_path} stop {app.unit}, "
        f"{systemctl_path} restart {app.unit}\n"
    )


def make_apps_unit_fixup(app: AppConfig) -> Fixup:
    filename = _apps_unit_sudoers_file(app.id)

    def check() -> bool:
        return (SUDOERS_DIR / filename).exists()

    def apply() -> None:
        systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        content = apps_unit_sudoers_content(app, systemctl, getuser())
        _install_sudoers_snippet(filename, content)

    return Fixup(
        id=f"apps-unit-sudoers-{app.id}",
        title=f"Разрешить управление «{app.title}» ({app.unit}) без пароля",
        needed=_apps_unit_needed,
        check=check,
        apply=apply,
    )


def build_fixups(settings: Settings) -> list[Fixup]:
    """Известные фиксы, актуальные для текущих назначений ноды (``needed``)."""
    fixups = [
        INSTALL_SMARTMONTOOLS,
        SMARTCTL_SUDOERS,
        JOURNALCTL_GROUP,
        *(make_apps_unit_fixup(app) for app in settings.apps.items),
    ]
    return [f for f in fixups if f.needed(settings)]
