from __future__ import annotations

import logging
import os
import shutil
import subprocess
from getpass import getuser
from pathlib import Path

from sa_home_bot import wol
from sa_home_bot.config import AppConfig
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.privileged import (
    SUDOERS_DIR,
    daemon_reload,
    enable_now,
    find_executable,
    install_package,
    install_root_file,
    install_sudoers_snippet,
    run_sudo,
)
from sa_home_bot.sensors.disks import SMARTCTL_REQUIREMENT

log = logging.getLogger(__name__)

SMARTCTL_SUDOERS_FILE = "50-sa-home-node-smartctl"
SMARTCTL_WRAPPER_PATH = Path.home() / ".local" / "bin" / "smartctl"
NODE_UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / "sa-home-node.service"
NODE_UNIT_NAME = "sa-home-node.service"
PATH_LINE_PREFIX = "Environment=PATH="
POWER_POLKIT_RULE_FILE = Path("/etc/polkit-1/rules.d/50-sa-home-node-power.rules")
POWER_POLKIT_PACKAGE = "polkitd"
POWER_POLKIT_ACTIONS = (
    "power-off",
    "power-off-multiple-sessions",
    "reboot",
    "reboot-multiple-sessions",
    "suspend",
    "suspend-multiple-sessions",
)
WOL_UNIT_FILE = Path("/etc/systemd/system/sa-home-wol.service")
WOL_UNIT_NAME = WOL_UNIT_FILE.name


def _smartmontools_check() -> bool:
    return find_executable("smartctl") is not None


def _smartmontools_apply() -> None:
    install_package(
        SMARTCTL_REQUIREMENT.package, "не найден известный пакетный менеджер для smartmontools"
    )


INSTALL_SMARTMONTOOLS = Fixup(
    id="install-smartmontools",
    title="Установить smartmontools",
    needed=needs.monitor_disks,
    check=_smartmontools_check,
    apply=_smartmontools_apply,
)


def real_smartctl_path() -> str | None:
    return find_executable("smartctl", exclude_dir=str(SMARTCTL_WRAPPER_PATH.parent))


def smartctl_sudoers_content(smartctl_path: str, user: str) -> str:
    return f"{user} ALL=(root) NOPASSWD: {smartctl_path} *\n"


def smartctl_wrapper_content(real_path: str) -> str:
    return f"#!/bin/sh\nexec sudo -n {real_path} \"$@\"\n"


def _smartctl_sudoers_check() -> bool:
    return (SUDOERS_DIR / SMARTCTL_SUDOERS_FILE).exists() and SMARTCTL_WRAPPER_PATH.exists()


def _smartctl_sudoers_apply() -> None:
    path = real_smartctl_path()
    if path is None:
        raise FixupError("smartctl не найден в PATH — сначала install-smartmontools")
    if not (SUDOERS_DIR / SMARTCTL_SUDOERS_FILE).exists():
        install_sudoers_snippet(SMARTCTL_SUDOERS_FILE, smartctl_sudoers_content(path, getuser()))
    if not SMARTCTL_WRAPPER_PATH.exists():
        SMARTCTL_WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
        SMARTCTL_WRAPPER_PATH.write_text(smartctl_wrapper_content(path))
        SMARTCTL_WRAPPER_PATH.chmod(0o755)


SMARTCTL_SUDOERS = Fixup(
    id="smartctl-sudoers",
    title="Разрешить smartctl без пароля (sudoers + обёртка ~/.local/bin)",
    needed=needs.monitor_disks,
    check=_smartctl_sudoers_check,
    apply=_smartctl_sudoers_apply,
)


def rewrite_unit_path_line(content: str, wrapper_dir: str) -> str | None:
    out: list[str] = []
    found = False
    for line in content.splitlines(keepends=True):
        if line.startswith(PATH_LINE_PREFIX):
            found = True
            newline = "\n" if line.endswith("\n") else ""
            rest = line[len(PATH_LINE_PREFIX) : len(line) - len(newline)]
            dirs = [d for d in rest.split(os.pathsep) if d and d != wrapper_dir]
            out.append(f"{PATH_LINE_PREFIX}{os.pathsep.join([wrapper_dir, *dirs])}{newline}")
        else:
            out.append(line)
    return "".join(out) if found else None


def _unit_path_first_dir(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith(PATH_LINE_PREFIX):
            return line[len(PATH_LINE_PREFIX) :].split(os.pathsep, 1)[0] or None
    return None


def _node_unit_path_check() -> bool:
    if not NODE_UNIT_FILE.exists():
        return True
    return _unit_path_first_dir(NODE_UNIT_FILE.read_text()) == str(SMARTCTL_WRAPPER_PATH.parent)


def _node_unit_path_apply() -> None:
    new_content = rewrite_unit_path_line(
        NODE_UNIT_FILE.read_text(), str(SMARTCTL_WRAPPER_PATH.parent)
    )
    if new_content is None:
        raise FixupError(f"в {NODE_UNIT_FILE} нет строки Environment=PATH= — правьте руками")
    NODE_UNIT_FILE.write_text(new_content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    result = subprocess.run(["systemctl", "--user", "restart", NODE_UNIT_NAME], check=False)
    if result.returncode != 0:
        log.warning(
            "PATH юнита поправлен, но `systemctl --user restart %s` не сработал — "
            "перезапустите ноду вручную",
            NODE_UNIT_NAME,
        )


NODE_UNIT_SMARTCTL_PATH = Fixup(
    id="node-unit-smartctl-path",
    title="Поправить PATH юнита sa-home-node (~/.local/bin первым, там обёртка smartctl)",
    needed=needs.monitor_disks,
    check=_node_unit_path_check,
    apply=_node_unit_path_apply,
)


def _in_group(group: str) -> bool:
    try:
        out = subprocess.run(["id", "-nG", getuser()], capture_output=True, text=True, check=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return group in out.stdout.split()


def _journalctl_group_check() -> bool:
    return _in_group("systemd-journal")


def _journalctl_group_apply() -> None:
    run_sudo(["usermod", "-aG", "systemd-journal", getuser()])
    log.warning(
        "Группа systemd-journal добавлена — применится после нового логина/сессии, "
        "не мгновенно в текущей."
    )


JOURNALCTL_GROUP = Fixup(
    id="journalctl-group",
    title="Добавить пользователя в группу systemd-journal",
    needed=needs.monitor,
    check=_journalctl_group_check,
    apply=_journalctl_group_apply,
)


def apps_unit_sudoers_content(app: AppConfig, systemctl_path: str, user: str) -> str:
    return (
        f"{user} ALL=(root) NOPASSWD: "
        f"{systemctl_path} start {app.unit}, "
        f"{systemctl_path} stop {app.unit}, "
        f"{systemctl_path} restart {app.unit}\n"
    )


def make_apps_unit_fixup(app: AppConfig) -> Fixup:
    filename = f"50-sa-home-node-apps-{app.id}"

    def check() -> bool:
        return (SUDOERS_DIR / filename).exists()

    def apply() -> None:
        systemctl = shutil.which("systemctl") or "/usr/bin/systemctl"
        install_sudoers_snippet(filename, apps_unit_sudoers_content(app, systemctl, getuser()))

    return Fixup(
        id=f"apps-unit-sudoers-{app.id}",
        title=f"Разрешить управление «{app.title}» ({app.unit}) без пароля",
        needed=needs.apps,
        check=check,
        apply=apply,
    )


def power_polkit_rule_content(user: str) -> str:
    ids = ",\n        ".join(f'"org.freedesktop.login1.{a}"' for a in POWER_POLKIT_ACTIONS)
    return (
        "// Сгенерировано sa-home-bot (nodectl fix) — нода-супервизор "
        "выключает/перезагружает/усыпляет свою workstation без sudo.\n"
        "polkit.addRule(function(action, subject) {\n"
        f"    if ([\n        {ids}\n    ].indexOf(action.id) !== -1 "
        f'&& subject.user == "{user}") {{\n'
        "        return polkit.Result.YES;\n"
        "    }\n"
        "});\n"
    )


def _power_control_check() -> bool:
    return POWER_POLKIT_RULE_FILE.exists()


def _power_control_apply() -> None:
    if not POWER_POLKIT_RULE_FILE.parent.is_dir():
        install_package(
            POWER_POLKIT_PACKAGE,
            "polkit не установлен, и неизвестен пакетный менеджер для установки "
            f"пакета {POWER_POLKIT_PACKAGE!r}",
        )
    run_sudo(["install", "-d", "-m", "0755", str(POWER_POLKIT_RULE_FILE.parent)])
    install_root_file(
        POWER_POLKIT_RULE_FILE, power_polkit_rule_content(getuser()), mode="0644"
    )


POWER_CONTROL_POLKIT = Fixup(
    id="power-control-polkit",
    title="Разрешить ноде выключать/перезагружать/усыплять машину без sudo (polkit)",
    needed=needs.power_controllable,
    check=_power_control_check,
    apply=_power_control_apply,
)


def wol_unit_content(ethtool_path: str, iface: str) -> str:
    return (
        "[Unit]\n"
        "Description=sa-home-bot: включить Wake-on-LAN (magic packet)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={ethtool_path} -s {iface} wol g\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _wol_check() -> bool:
    return WOL_UNIT_FILE.exists()


def _wol_apply() -> None:
    iface = wol.detect_local_wake_iface()
    if iface is None:
        raise FixupError(
            "не нашёл проводной Ethernet-интерфейс по умолчанию — WoL настраивать не на чем"
        )
    ethtool_path = find_executable("ethtool")
    if ethtool_path is None:
        install_package(
            "ethtool", "ethtool не найден и неизвестен пакетный менеджер для установки"
        )
        ethtool_path = find_executable("ethtool")
        if ethtool_path is None:
            raise FixupError("ethtool не нашёлся после установки")
    install_root_file(WOL_UNIT_FILE, wol_unit_content(ethtool_path, iface), mode="0644")
    daemon_reload()
    enable_now(WOL_UNIT_NAME)


WOL_ENABLE = Fixup(
    id="wol-enable",
    title="Включить Wake-on-LAN (magic packet) на проводном интерфейсе",
    needed=needs.wakeable,
    check=_wol_check,
    apply=_wol_apply,
)
