"""Рецепты повышения привилегий, выполняемые вручную по SSH (`nodectl fix`).

Короткоживущий процесс, не демон: читает конфиг локально (как остальной
`nodectl`), определяет, какие фиксы нужны исходя из назначений ноды, и для
каждого непройденного `check()` зовёт настоящий интерактивный `sudo`
(наследует TTY — пароль нигде не хранится и никуда не передаётся по сети).
Долгоживущий процесс ноды (`node/service.py`) сам `sudo` не вызывает и прав
не хранит — этот инвариант fixups не нарушают.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path

from sa_home_bot import wol
from sa_home_bot.bot import vpn_nodes
from sa_home_bot.config import AppConfig, Settings
from sa_home_bot.node import assignments, vpn_probe_state
from sa_home_bot.node import kind as node_kinds
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import resolve_endpoint
from sa_home_bot.sensors.disks import SMARTCTL_REQUIREMENT
from sa_home_bot.utils.requirements import install_argv
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

SUDOERS_DIR = Path("/etc/sudoers.d")

# `nodectl fix` запускают интерактивно по SSH под обычным логином — его PATH
# (в отличие от PATH юнита ноды, deploy/sa-home-node.service) обычно НЕ
# включает /usr/sbin и /sbin, где пакеты кладут smartctl, visudo и т.п.
# Фолбэк туда — иначе `shutil.which` их не находит, хотя они установлены.
_SBIN_FALLBACK_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")


def _which(name: str) -> str | None:
    """``shutil.which`` с фолбэком на типовые sbin-каталоги (см. выше)."""
    found = shutil.which(name)
    if found is not None:
        return found
    for d in _SBIN_FALLBACK_DIRS:
        candidate = Path(d) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


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
    try:
        result = subprocess.run(["sudo", *argv])
    except OSError as exc:
        raise FixupError(f"не удалось запустить sudo {' '.join(argv)}: {exc}") from exc
    if result.returncode != 0:
        raise FixupError(f"sudo {' '.join(argv)} завершился кодом {result.returncode}")


def _run(argv: list[str], **kwargs: object) -> None:
    """Непривилегированный подпроцесс (git/go/make) с диагнозом в FixupError."""
    try:
        result = subprocess.run(argv, **kwargs)
    except OSError as exc:
        raise FixupError(f"не удалось запустить {' '.join(argv)}: {exc}") from exc
    if result.returncode != 0:
        raise FixupError(f"{' '.join(argv)} завершился кодом {result.returncode}")


def _privileged_exists(path: Path) -> bool:
    """``Path.exists()``, терпимый к каталогам без прав на просмотр обычным
    пользователем (напр. ``/etc/amnezia/amneziawg`` — ``setup-awg-jeeves.sh``
    создаёт его под ``umask 077``, т.е. 0700 root:root, и это ломает голый
    ``stat`` даже для ФАЙЛА, которого там ещё нет). PermissionError здесь —
    не «файла нет», а «не видно» — переспрашиваем через sudo, а не отвечаем
    вслепую, иначе ``apply()`` решит, что конфига нет, и выпросит у vpn@jeeves
    новый поверх уже рабочего."""
    try:
        return path.exists()
    except PermissionError:
        pass
    try:
        result = subprocess.run(["sudo", "test", "-e", str(path)])
    except OSError as exc:
        raise FixupError(f"не удалось проверить {path} через sudo: {exc}") from exc
    return result.returncode == 0


def _install_sudoers_snippet(name: str, content: str) -> None:
    """Валидировать содержимое через ``visudo`` и установить файл под sudo."""
    visudo = _which("visudo")
    if visudo is None:
        raise FixupError("visudo не найден (проверьте установку пакета sudo)")
    with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        try:
            check = subprocess.run(
                [visudo, "-cf", str(tmp_path)], capture_output=True, text=True
            )
        except OSError as exc:
            raise FixupError(f"не удалось запустить visudo: {exc}") from exc
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
    return (
        assignments.has_service(settings.node.assignments, "monitor")
        and settings.sensors.disks.enabled
    )


def _smartmontools_check() -> bool:
    # _which(), не голый shutil.which(): интерактивный логин-шелл по SSH
    # обычно не включает /usr/sbin, где apt кладёт smartctl (см. _which) —
    # тем же путём, что smartctl-sudoers уже резолвит настоящий бинарник,
    # иначе фикс вечно считает пакет неприменённым (живой баг 2026-08-01
    # на mycraft: apt честно отвечает «уже установлен», а check() — нет).
    return _which("smartctl") is not None


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


# --- smartctl: узкий sudoers-снипет + обёртка в PATH (NOPASSWD на конкретный бинарник) ---
#
# Само по себе право sudo без пароля ничего не даёт: код (sensors/disks.py) зовёт
# голое `smartctl`, sudo не добавляет. Юнит ноды кладёт ~/.local/bin первым в PATH
# (см. deploy/sa-home-node.service) специально ради обёртки-скрипта, которая молча
# перенаправляет вызов в `sudo -n <настоящий smartctl>`. Оба шага нужны вместе.

SMARTCTL_SUDOERS_FILE = "50-sa-home-node-smartctl"
SMARTCTL_WRAPPER_PATH = Path.home() / ".local" / "bin" / "smartctl"


def _real_smartctl_path() -> str | None:
    """Резолвит настоящий smartctl: игнорирует нашу же обёртку в PATH (иначе
    повторный запуск фикса поставил бы sudoers-снипет на сам скрипт-обёртку)
    и добавляет фолбэк на sbin-каталоги (см. ``_which``) — интерактивный PATH
    обычного пользователя обычно не включает /usr/sbin, где smartmontools
    ставит бинарник."""
    wrapper_dir = str(SMARTCTL_WRAPPER_PATH.parent)
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d != wrapper_dir]
    found = shutil.which("smartctl", path=os.pathsep.join(dirs))
    if found is not None:
        return found
    for d in _SBIN_FALLBACK_DIRS:
        candidate = Path(d) / "smartctl"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def smartctl_sudoers_content(smartctl_path: str, user: str) -> str:
    """Содержимое sudoers-снипета: NOPASSWD только на резолвленный путь
    smartctl (не голое имя — защита от PATH-hijack), с любыми аргументами."""
    return f"{user} ALL=(root) NOPASSWD: {smartctl_path} *\n"


def smartctl_wrapper_content(real_path: str) -> str:
    """Скрипт-обёртка ~/.local/bin/smartctl: прозрачно зовёт настоящий smartctl
    под root через sudo. Код продолжает звать голое `smartctl` — не знает про sudo."""
    return f"#!/bin/sh\nexec sudo -n {real_path} \"$@\"\n"


def _smartctl_sudoers_check() -> bool:
    return (SUDOERS_DIR / SMARTCTL_SUDOERS_FILE).exists() and SMARTCTL_WRAPPER_PATH.exists()


def _smartctl_sudoers_apply() -> None:
    path = _real_smartctl_path()
    if path is None:
        raise FixupError("smartctl не найден в PATH — сначала install-smartmontools")
    if not (SUDOERS_DIR / SMARTCTL_SUDOERS_FILE).exists():
        _install_sudoers_snippet(SMARTCTL_SUDOERS_FILE, smartctl_sudoers_content(path, getuser()))
    if not SMARTCTL_WRAPPER_PATH.exists():
        SMARTCTL_WRAPPER_PATH.parent.mkdir(parents=True, exist_ok=True)
        SMARTCTL_WRAPPER_PATH.write_text(smartctl_wrapper_content(path))
        SMARTCTL_WRAPPER_PATH.chmod(0o755)


SMARTCTL_SUDOERS = Fixup(
    id="smartctl-sudoers",
    title="Разрешить smartctl без пароля (sudoers + обёртка ~/.local/bin)",
    needed=_smartmontools_needed,
    check=_smartctl_sudoers_check,
    apply=_smartctl_sudoers_apply,
)


# --- юнит ноды: ~/.local/bin первым в PATH (там обёртка smartctl) ---
#
# `sa-home-bot init` кладёт эту строку сам (см. setup_wizard._render_systemd_unit),
# но баг там же (до фикса 2026-08-08) мог развернуть путь исполняемого файла до
# каталога pipx-venv вместо ~/.local/bin — юнит уже установлен без обёртки в
# PATH, и smartctl-sudoers (выше) сам это не чинит: он трогает только сниппет
# и обёртку, не юнит. Этот фикс чинит уже развёрнутые юниты той же командой.

NODE_UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / "sa-home-node.service"
NODE_UNIT_NAME = "sa-home-node.service"


def rewrite_unit_path_line(content: str, wrapper_dir: str) -> str | None:
    """Переписать ``Environment=PATH=`` так, чтобы ``wrapper_dir`` шёл первым
    (дубли самого себя убираются). ``None``, если такой строки в юните нет."""
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    found = False
    for line in lines:
        if line.startswith("Environment=PATH="):
            found = True
            newline = "\n" if line.endswith("\n") else ""
            rest = line[len("Environment=PATH=") : len(line) - len(newline)]
            dirs = [d for d in rest.split(os.pathsep) if d and d != wrapper_dir]
            out.append(f"Environment=PATH={os.pathsep.join([wrapper_dir, *dirs])}{newline}")
        else:
            out.append(line)
    return "".join(out) if found else None


def _unit_path_first_dir(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("Environment=PATH="):
            return line[len("Environment=PATH=") :].split(os.pathsep, 1)[0] or None
    return None


def _node_unit_path_check() -> bool:
    if not NODE_UNIT_FILE.exists():
        return True  # юнит ещё не создан — sa-home-bot init допишет PATH сам
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
    needed=_smartmontools_needed,
    check=_node_unit_path_check,
    apply=_node_unit_path_apply,
)


# --- journalctl: доступ к журналу без root (группа systemd-journal) ---


def _journalctl_needed(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "monitor")


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
    return assignments.has_service(settings.node.assignments, "apps")


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


# --- power control: разрешить ноде выключать/перезагружать/усыплять машину
# без sudo (только workstation — см. NodeTraits.power_controllable) ---
#
# node/service.py вызывает голое `systemctl poweroff/reboot/suspend` (см.
# инвариант в шапке файла: долгоживущий процесс сам sudo не зовёт). Обычный
# пользователь без активной локальной сессии (systemd --user юнит, вход по
# SSH) не может это сделать без разрешения — по умолчанию logind/polkit
# спросят интерактивную аутентификацию, которую подать некому. Правило
# polkit — то же самое разрешение, что получает пользователь за физическим
# экраном на «Выключить», просто явно для этого логина.

POWER_POLKIT_RULE_FILE = Path("/etc/polkit-1/rules.d/50-sa-home-node-power.rules")
# Пакет называется polkitd на Debian/Ubuntu (policykit-1 там же — пустой
# переходный пакет, apt-cache policy отдаёт "Кандидат: (отсутствует)"); на
# прочих дистрибутивах это просто polkit. Минимальная установка сервера
# (headless, без DE) его вообще не тянет — живой баг 2026-08-01 на mycraft:
# каталога /etc/polkit-1 не было вовсе, install падал с ENOENT.
POWER_POLKIT_PACKAGE = "polkitd"
_POWER_POLKIT_ACTIONS = (
    "power-off",
    "power-off-multiple-sessions",
    "reboot",
    "reboot-multiple-sessions",
    "suspend",
    "suspend-multiple-sessions",
)


def _power_control_needed(settings: Settings) -> bool:
    return node_kinds.traits_for(settings.node.kind).power_controllable


def power_polkit_rule_content(user: str) -> str:
    """Содержимое правила polkit: только перечисленные login1-действия и
    только для ``user`` — не carte blanche на остальные действия polkit."""
    ids = ",\n        ".join(f'"org.freedesktop.login1.{a}"' for a in _POWER_POLKIT_ACTIONS)
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
        argv = install_argv(POWER_POLKIT_PACKAGE)
        if argv is None:
            raise FixupError(
                f"polkit не установлен, и неизвестен пакетный менеджер для установки "
                f"пакета {POWER_POLKIT_PACKAGE!r}"
            )
        _sudo(argv)
    content = power_polkit_rule_content(getuser())
    with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        # -d на случай, если пакет почему-то не создал rules.d сам (или его
        # вовсе нет — см. комментарий у POWER_POLKIT_PACKAGE).
        _sudo(["install", "-d", "-m", "0755", str(POWER_POLKIT_RULE_FILE.parent)])
        _sudo(
            [
                "install",
                "-m",
                "0644",
                "-o",
                "root",
                "-g",
                "root",
                str(tmp_path),
                str(POWER_POLKIT_RULE_FILE),
            ]
        )
    finally:
        tmp_path.unlink(missing_ok=True)


POWER_CONTROL_POLKIT = Fixup(
    id="power-control-polkit",
    title="Разрешить ноде выключать/перезагружать/усыплять машину без sudo (polkit)",
    needed=_power_control_needed,
    check=_power_control_check,
    apply=_power_control_apply,
)


# --- Wake-on-LAN: включить приём magic packet на проводном интерфейсе
# (только workstation — этой машине штатно быть выключенной и просыпаться) ---

WOL_UNIT_FILE = Path("/etc/systemd/system/sa-home-wol.service")
WOL_UNIT_NAME = WOL_UNIT_FILE.name


def _wol_needed(settings: Settings) -> bool:
    return node_kinds.traits_for(settings.node.kind).wakeable


def wol_unit_content(ethtool_path: str, iface: str) -> str:
    """systemd-юнит вместо правки /etc/network/interfaces — работает
    одинаково под ifupdown, NetworkManager и systemd-networkd, не зависит от
    того, чем на конкретной машине управляется сеть."""
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
    ethtool_path = _which("ethtool")
    if ethtool_path is None:
        argv = install_argv("ethtool")
        if argv is None:
            raise FixupError("ethtool не найден и неизвестен пакетный менеджер для установки")
        _sudo(argv)
        ethtool_path = _which("ethtool")
        if ethtool_path is None:
            raise FixupError("ethtool не нашёлся после установки")
    content = wol_unit_content(ethtool_path, iface)
    with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        _sudo(
            ["install", "-m", "0644", "-o", "root", "-g", "root", str(tmp_path), str(WOL_UNIT_FILE)]
        )
    finally:
        tmp_path.unlink(missing_ok=True)
    _sudo(["systemctl", "daemon-reload"])
    _sudo(["systemctl", "enable", "--now", WOL_UNIT_NAME])


WOL_ENABLE = Fixup(
    id="wol-enable",
    title="Включить Wake-on-LAN (magic packet) на проводном интерфейсе",
    needed=_wol_needed,
    check=_wol_check,
    apply=_wol_apply,
)


# --- vpn: узкий sudoers на `awg show`/`awg set <iface> peer` (Этап 33) ---
#
# Долгоживущий процесс службы vpn (node/service.py::VpnService, тот же
# инвариант «сам sudo не зовёт») правит пиров AmneziaWG на jeeves. Root ноде
# не отдаём: сниппет разрешает ровно `awg show *` (чтение счётчиков,
# handshake, публичного ключа интерфейса) и `awg set <iface> peer *`
# (добавить/снять пира и его allowed-ips) — НЕ `awg-quick` целиком (он
# исполняет произвольные PostUp из конфига = равносилен root) и НЕ
# `awg set <iface> private-key/listen-port` (эти параметры трогает только
# ops-скрипт при установке, не служба). Путь резолвится через `_which` —
# защита от PATH-hijack, тот же приём, что у smartctl_sudoers_content.

AWG_SUDOERS_FILE = "50-sa-home-node-awg"


def _vpn_needed(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "vpn")


def _vpn_awg_needed(settings: Settings) -> bool:
    """Служба vpn с транспортом AmneziaWG на этой ноде — только тогда нужны
    awg-sudoers и прокси-демоны (mtg/microsocks). Нода с одним лишь
    транспортом reality (VLESS через xray) их не касается: xray ходит по
    своему gRPC API без sudo, а прокси Telegram там не разворачивается
    (подэтап 39.0.x)."""
    return _vpn_needed(settings) and (
        vpn_protocol.TRANSPORT_AWG in (settings.vpn.transports or [vpn_protocol.TRANSPORT_AWG])
    )


def awg_sudoers_content(awg_path: str, interface: str, user: str) -> str:
    return (
        f"{user} ALL=(root) NOPASSWD: {awg_path} show *, "
        f"{awg_path} set {interface} peer *\n"
    )


def _awg_sudoers_check() -> bool:
    return (SUDOERS_DIR / AWG_SUDOERS_FILE).exists()


def _awg_sudoers_apply(settings: Settings) -> None:
    path = _which("awg")
    if path is None:
        raise FixupError(
            "awg не найден в PATH — сначала поставьте amneziawg-tools "
            "(см. deploy/setup-awg-jeeves.sh)"
        )
    content = awg_sudoers_content(path, settings.vpn.interface, getuser())
    _install_sudoers_snippet(AWG_SUDOERS_FILE, content)


def make_awg_sudoers_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="awg-sudoers",
        title="Разрешить управление awg без пароля (sudoers, только show/set peer)",
        needed=_vpn_awg_needed,
        check=_awg_sudoers_check,
        apply=lambda: _awg_sudoers_apply(settings),
    )


# --- vpn_check: клиентские туннели-пробники (netns + veth + NAT, сам
# туннель — эфемерно) ---
#
# Служба vpn_check (vpn_check/service.py) сама awg-quick/xray не поднимает
# насовсем и root не получает (тот же инвариант «долгоживущий процесс сам
# sudo не зовёт», что у остальных служб) — готовые netns+veth+NAT заводит
# заранее этот фикс, а сам процесс туннеля служба поднимает/гасит вокруг
# каждого чек-цикла (см. докстринг vpn_check/service.py). Конфиг (приватный
# ключ) получаем ровно тем же способом, каким его получил бы обычный гость:
# зовём action "issue" службы vpn на КОНКРЕТНОМ сервере slot.server с
# chat_id=0 (см. vpn/service.py::NODE_SENTINEL_CHAT_ID — уже зарезервирован
# под "не гостя, а саму ноду"; отдельного "системного" действия в VpnService
# заводить не понадобилось, решение пользователя 2026-08-17: `_issue()` не
# проверяет принадлежность chat_id реальному чату, это просто ключ учёта в
# БД).
#
# Решение пользователя 2026-08-18: проверка обязана быть ОДИНАКОВОЙ на любой
# ноде роя (правило роя — никаких «эта нода особенная») и обязана проверять
# реальную работоспособность VPN как её видит настоящий клиент, а не
# суррогат вроде чтения состояния nftables/awg. Поэтому интерфейс живёт
# ИЗОЛИРОВАННО в собственном network namespace на КАЖДОЙ ноде с vpn_check —
# vpn_check/service.py всегда зовёт curl через ``ip netns exec``, без
# исключений и без node-specific веток кода.
#
# С 39.0.7(d) список целей — не хардкод «jeeves/awg» и не ручной
# ``[vpn_check] probe_server``, а автообнаружение (все живые vpn-серверы
# роя кроме себя, ``bot/vpn_nodes.py::probe_targets``): у каждой пары
# (сервер, транспорт) свой netns/veth/подсеть/интерфейс, имена выводятся
# детерминированно из позиции пары в отсортированном списке (см.
# ``_probe_slot_from_target``). Что реально настроено — записывает
# ``node/vpn_probe_state.py`` (root пишет, обычный пользователь читает),
# служба vpn_check сама сеть не провижинит, только пользуется готовым.
#
# Живые находки 2026-08-17/2026-08-18 про то, каким должен быть путь netns
# наружу:
#
# 1) Голый ``ip netns add`` без ничего внутри не имеет НИ ОДНОГО физического
#    интерфейса — WireGuard-хендшейку (обычный, не туннелируемый UDP до
#    эндпоинта сервера) решительно некуда уйти в интернет: `ip route show`
#    внутри netns был пуст, `awg show` не показывал ни одного хендшейка, все
#    проверки висели по таймауту (curl exit 28).
#
# 2) На ноде, где крутится сам VPN-сервер, есть ДОПОЛНИТЕЛЬНАЯ причина,
#    почему голый netns без выделенного пути наружу никогда не заработал бы:
#    адрес клиента-пробника (из подсети VPN, напр. 10.9.0.14) неизбежно
#    совпадает с адресом, который на этой же машине обслуживает сервер —
#    то есть с точки зрения root netns он «свой». Когда awg0 расшифровывает
#    пакет пробника и пытается переслать его на внешний интерфейс, ядро
#    видит source-адрес, совпадающий с локальным, и БЕЗУСЛОВНО отвергает
#    пересылку (`fib_validate_source`, martian source — не зависит от
#    rp_filter и не лечится sysctl `accept_local`: проверено вручную —
#    `accept_local=1` чинит саму FIB-проверку (`ip route get ... iif awg0`
#    перестаёт падать), но пакет всё равно не доходит до
#    postrouting/NAT, экспериментально подтверждено трассировкой `nft
#    monitor trace`). Единственный чистый способ снять это ограничение —
#    сделать так, чтобы адрес пробника НЕ БЫЛ «своим» с точки зрения root
#    netns вообще, т.е. изолировать его в другом netns.
#
# И (1), и (2) решаются ОДНИМ и тем же приёмом, одинаковым на любой ноде и
# на любую пару (сервер, транспорт): veth-пара между root netns и netns
# пробника + точечный NAT/forward на хосте (см. _resolve_probe_forward_
# targets ниже). Внутри netns пробника awg-quick дальше работает ПО
# УМОЛЧАНИЮ (без Table=off) — сам заводит fwmark/ip rule и объявляет себя
# default route ВНУТРИ netns, это ничему не мешает, потому что весь netns и
# так предназначен только для пробника. Живьём проверено на jeeves ручным
# стендом (netns+veth+NAT, без правки прод-юнита) 2026-08-18: и 1.1.1.1, и
# api.telegram.org отвечают полным TLS/HTTP через собранный так туннель —
# включая DNS для api.telegram.org: он неожиданно разрешается без всякого
# ручного /etc/netns/<netns>/resolv.conf (Tailscale MagicDNS 100.100.100.100
# из /etc/resolv.conf прозрачно достижим через forward-правило veth — тот же
# hairpin-приём, каким сама нода достаёт себя по 100.100.100.100).

VPN_PROBE_ROUTE_SENTINEL = "1.1.1.1"
VPN_PROBE_AWG_CONF_DIR = Path("/etc/amnezia/amneziawg")
VPN_PROBE_SCAFFOLD_UNIT_DIR = Path("/etc/systemd/system")
VPN_PROBE_SUDOERS_FILE = "50-sa-home-node-vpn-probe"
# vpn/service.py::NODE_SENTINEL_CHAT_ID — не импортируем службу целиком
# (тяжёлые зависимости: БД, awg-бэкенд) ради одной константы, тот же приём,
# что уже используют node/peers.py и другие (свой NODE_SERVICE = "node").
VPN_PROBE_CHAT_ID = 0

# veth-пары — единственный путь наружу для изолированных netns пробников
# (см. большой комментарий выше). Имена ≤15 символов (IFNAMSIZ), выводятся
# из индекса пары в отсортированном списке целей (``_probe_slot_from_
# target``). Подсеть 10.200.200.0/24 нарезается на /30 по числу целей — не
# пересекается ни с VPN 10.9.0.0/24, ни с типичным домашним LAN
# 192.168.0.0/24, ни с Tailscale CGNAT 100.64.0.0/10.
VPN_PROBE_SUBNET_BASE = "10.200.200"
# Название нашей собственной nft-таблицы — заводим её, только если на ноде
# не нашлось уже существующей форвардящей/nat-цепочки (см.
# _find_base_chain) — на jeeves она есть (inet filter/ip nat, из инцидентов
# 2026-08-04/2026-08-17), на прочих нодах обычно нет вообще никакого
# firewall, и создание своей маленькой таблицы с политикой accept ничего не
# меняет для остального трафика ноды.
VPN_PROBE_NFT_TABLE = "sa_vpn_probe"


def _vpn_check_needed(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "vpn_check")


def _probe_slot_from_target(index: int, target: vpn_nodes.ProbeTarget) -> vpn_probe_state.ProbeSlot:
    """Имена/подсеть детерминированы позицией пары в отсортированном списке
    целей — стабильны между прогонами `nodectl fix`, пока состав целей не
    меняется (см. IMPLEMENTATION_PLAN.md, 39.0.7(d)). Транспорты, которые
    этот фикс ещё не умеет провижинить (reality — до 39.0.7(e)), тоже
    получают слот (``iface``/``socks_port`` соответственно ``None``) —
    номер слота остаётся зарезервирован и не съезжает у awg-пар, когда
    reality-фиксы появятся."""
    netns = f"vpn-probe-{target.server}-{target.transport}"
    veth_host = f"vprobe{index}h0"
    veth_ns = f"vprobe{index}n0"
    base = index * 4
    veth_host_addr = f"{VPN_PROBE_SUBNET_BASE}.{base + 1}/30"
    veth_ns_addr = f"{VPN_PROBE_SUBNET_BASE}.{base + 2}/30"
    subnet = f"{VPN_PROBE_SUBNET_BASE}.{base}/30"
    iface = f"awg-probe{index}" if target.transport == "awg" else None
    socks_port = None if target.transport == "awg" else 11080 + index
    return vpn_probe_state.ProbeSlot(
        server=target.server,
        transport=target.transport,
        netns=netns,
        veth_host=veth_host,
        veth_ns=veth_ns,
        veth_host_addr=veth_host_addr,
        veth_ns_addr=veth_ns_addr,
        subnet=subnet,
        iface=iface,
        socks_port=socks_port,
    )


async def _fetch_probe_slots(settings: Settings) -> list[vpn_probe_state.ProbeSlot]:
    endpoint = resolve_endpoint(settings.node.socket)
    client = ProtoClient(endpoint, token=settings.swarm.token)
    try:
        await client.connect()
        targets = await vpn_nodes.probe_targets(client, exclude=socket.gethostname())
    finally:
        await client.close()
    return [_probe_slot_from_target(i, t) for i, t in enumerate(targets)]


def _discover_probe_slots(settings: Settings) -> list[vpn_probe_state.ProbeSlot]:
    """Все (сервер, транспорт), которые эта нода должна проверять — все
    живые vpn-серверы роя кроме себя. Синхронная обёртка (``nodectl fix`` —
    короткоживущий процесс, не asyncio-приложение); сетевой сбой сведён к
    пустому списку с предупреждением, а не падению всего `nodectl fix` —
    фиксы, не связанные с VPN, не должны зависеть от доступности роя (тот
    же принцип, что уже применялся к одиночному ``_fetch_probe_config``)."""
    try:
        return asyncio.run(_fetch_probe_slots(settings))
    except Exception as exc:  # noqa: BLE001 — сеть/протокол сведены к одному диагнозу
        log.warning("nodectl fix: не удалось обнаружить VPN-серверы для пробника: %s", exc)
        return []


def _probe_scaffold_unit_path(slot: vpn_probe_state.ProbeSlot) -> Path:
    name = f"sa-home-vpn-probe-scaffold-{slot.server}-{slot.transport}.service"
    return VPN_PROBE_SCAFFOLD_UNIT_DIR / name


def vpn_probe_scaffold_unit_content(slot: vpn_probe_state.ProbeSlot, ip_path: str) -> str:
    """Только «вечная» часть — netns + veth-пара + маршрут ВНУТРИ netns.
    Сам туннель (awg-quick/xray) сюда сознательно не входит — служба
    vpn_check поднимает и гасит его вокруг каждого чек-цикла (эфемерно,
    компромисс ради слабого железа — решение владельца 2026-09-18).
    ``ExecStartPre`` идемпотентно (ведущий ``-`` — не валить юнит, если шаг
    уже применён: повторный ``add`` уже существующего netns/veth просто
    вернёт ошибку, которую здесь намеренно игнорируем). Пересоздаётся на
    каждом чистом буте: ядро не хранит netns/veth между перезагрузками, а
    ``RemainAfterExit`` здесь не спасает (это состояние ЮНИТА, не состояние
    ядра)."""
    host_addr_only = slot.veth_host_addr.split("/")[0]
    pre_steps = [
        f"-{ip_path} netns add {slot.netns}",
        f"-{ip_path} link add {slot.veth_host} type veth peer name {slot.veth_ns}",
        f"-{ip_path} link set {slot.veth_ns} netns {slot.netns}",
        f"-{ip_path} addr add {slot.veth_host_addr} dev {slot.veth_host}",
        f"{ip_path} link set {slot.veth_host} up",
        f"-{ip_path} netns exec {slot.netns} {ip_path} addr add {slot.veth_ns_addr} "
        f"dev {slot.veth_ns}",
        f"{ip_path} netns exec {slot.netns} {ip_path} link set {slot.veth_ns} up",
        f"{ip_path} netns exec {slot.netns} {ip_path} link set lo up",
        f"-{ip_path} netns exec {slot.netns} {ip_path} route add default via {host_addr_only}",
    ]
    pre_lines = "".join(f"ExecStartPre={step}\n" for step in pre_steps)
    return (
        "[Unit]\n"
        "Description=sa-home-bot: netns-скаффолд VPN-пробника "
        f"({slot.server}/{slot.transport}, netns {slot.netns})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"{pre_lines}"
        "ExecStart=/bin/true\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _probe_scaffold_check(slot: vpn_probe_state.ProbeSlot) -> bool:
    unit_path = _probe_scaffold_unit_path(slot)
    if not _privileged_exists(unit_path):
        return False
    ip_path = _which("ip")
    if ip_path is None:
        return False
    expected = vpn_probe_scaffold_unit_content(slot, ip_path)
    try:
        current = _read_privileged(unit_path)
    except FixupError:
        return False
    if current != expected:
        return False
    return subprocess.run(["systemctl", "is-active", "--quiet", unit_path.name]).returncode == 0


def _probe_scaffold_apply(slot: vpn_probe_state.ProbeSlot) -> None:
    ip_path = _which("ip")
    if ip_path is None:
        raise FixupError("ip (iproute2) не найден в PATH")
    unit_path = _probe_scaffold_unit_path(slot)
    unit_content = vpn_probe_scaffold_unit_content(slot, ip_path)
    current_unit = _read_privileged(unit_path) if _privileged_exists(unit_path) else None
    if current_unit != unit_content:
        if current_unit is not None:
            _sudo(["systemctl", "stop", unit_path.name])
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as tmp:
            tmp.write(unit_content)
            tmp_path = Path(tmp.name)
        try:
            _sudo(
                ["install", "-m", "0644", "-o", "root", "-g", "root", str(tmp_path), str(unit_path)]
            )
        finally:
            tmp_path.unlink(missing_ok=True)
        _sudo(["systemctl", "daemon-reload"])
    _sudo(["systemctl", "enable", "--now", unit_path.name])


def make_vpn_probe_scaffold_fixup(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> Fixup:
    return Fixup(
        id=f"vpn-probe-scaffold-{slot.server}-{slot.transport}",
        title=f"Поднять netns-скаффолд пробника к {slot.server}/{slot.transport} (netns+veth)",
        needed=_vpn_check_needed,
        check=lambda: _probe_scaffold_check(slot),
        apply=lambda: _probe_scaffold_apply(slot),
    )


# amneziawg-tools/amneziawg-go нет апт-пакетом ни в одном стандартном
# репозитории Debian (проверено на alfred 2026-08-17 — `apt-get install
# amneziawg-tools` падает "Unable to find package") и апстрим не публикует
# свой apt-репозиторий (README amneziawg-tools описывает только `make &&
# make install`). Единственный рабочий путь — собрать из исходников, тем же
# способом, каким `deploy/setup-awg-jeeves.sh` вручную поднял сервер на
# jeeves: официальный тарбол Go (системный слишком старый — go.mod требует
# go 1.25+) + `git clone`/`make install` в /usr/local/bin. awg-quick сам
# находит amneziawg-go в PATH как userspace-фолбэк, когда нет kernel-модуля
# (src/wg-quick/linux.bash::add_if — `command -v amneziawg-go`), отдельный
# env var не нужен.

_GO_MIN_VERSION = (1, 25)
_AMNEZIAWG_GO_REPO = "https://github.com/amnezia-vpn/amneziawg-go"
_AMNEZIAWG_TOOLS_REPO = "https://github.com/amnezia-vpn/amneziawg-tools"


def _go_version_ok(go_bin: str) -> bool:
    try:
        result = subprocess.run([go_bin, "version"], capture_output=True, text=True)
    except OSError:
        return False
    match = re.search(r"go(\d+)\.(\d+)", result.stdout)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2))) >= _GO_MIN_VERSION


def _ensure_go(build_dir: Path) -> str:
    """Путь к go: системный, если версии хватает, иначе — официальный тарбол
    во временный каталог (системный Go не трогаем, как и оригинальный
    bash-скрипт — незачем менять то, чем может пользоваться остальная ОС)."""
    system_go = _which("go")
    if system_go is not None and _go_version_ok(system_go):
        return system_go
    version_probe = subprocess.run(
        ["curl", "-fsSL", "https://go.dev/VERSION?m=text"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version_probe.returncode != 0 or not version_probe.stdout.strip():
        raise FixupError(
            f"не удалось узнать актуальную версию Go: {version_probe.stderr.strip()}"
        )
    go_version = version_probe.stdout.splitlines()[0].strip()
    tarball = build_dir / "go.tar.gz"
    _run(
        ["curl", "-fsSL", f"https://go.dev/dl/{go_version}.linux-amd64.tar.gz", "-o", str(tarball)],
        timeout=180,
    )
    _run(["tar", "-C", str(build_dir), "-xzf", str(tarball)])
    return str(build_dir / "go" / "bin" / "go")


def _build_amneziawg_tools() -> None:
    """Собрать и поставить amneziawg-go + amneziawg-tools (awg, awg-quick) из
    исходников в /usr/local/bin. Идемпотентно: пропускает то, что уже есть в
    PATH."""
    for prog in ("git", "curl", "tar"):
        if _which(prog) is None:
            raise FixupError(f"{prog} не найден — установите вручную и повторите nodectl fix")
    if _which("gcc") is None or _which("make") is None:
        argv = install_argv("build-essential")
        if argv is None:
            raise FixupError(
                "gcc/make не найдены и неизвестен пакетный менеджер для build-essential"
            )
        _sudo(argv)

    with tempfile.TemporaryDirectory(prefix="sa-home-awg-build-") as build_dir_str:
        build_dir = Path(build_dir_str)
        go_bin = _ensure_go(build_dir)
        env = dict(os.environ)
        env["PATH"] = f"{Path(go_bin).parent}:{env.get('PATH', '')}"

        if _which("amneziawg-go") is None:
            src = build_dir / "amneziawg-go"
            _run(["git", "clone", "--depth", "1", _AMNEZIAWG_GO_REPO, str(src)])
            _run(["make", "-C", str(src)], env=env)
            _sudo(
                ["install", "-m", "0755", str(src / "amneziawg-go"), "/usr/local/bin/amneziawg-go"]
            )

        if _which("awg") is None:
            src = build_dir / "amneziawg-tools"
            _run(["git", "clone", "--depth", "1", _AMNEZIAWG_TOOLS_REPO, str(src)])
            _run(["make", "-C", str(src / "src")], env=env)
            _sudo(["make", "-C", str(src / "src"), "install"])


def _read_privileged(path: Path) -> str:
    result = subprocess.run(["sudo", "cat", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise FixupError(f"не удалось прочитать {path} через sudo: {result.stderr.strip()}")
    return result.stdout


def _prepare_probe_conf(config_text: str) -> str:
    """Две правки конфига, выданного vpn@<сервер> как обычному гостю:

    - без строки ``DNS =`` — awg-quick иначе сам зовёт ``resolvconf``,
      которого нет на Debian, и весь скрипт падает (живая находка
      2026-08-17). DNS пробнику отдельно решать не нужно: интерфейс живёт в
      СВОЁМ netns, но resolv.conf там — тот же, что на хосте (``ip netns
      exec`` не подменяет его без ``/etc/netns/<netns>/resolv.conf``,
      которого мы не заводим), а хостовый DNS достижим через veth+forward —
      проверено живьём на jeeves 2026-08-18 (Tailscale MagicDNS
      100.100.100.100 резолвит api.telegram.org из netns пробника без
      какого-либо дополнительного шиминга).
    - без строки ``Table`` (в любом виде — раньше сюда осознанно писали
      ``Table = off``, версии v0.92.2–v0.92.4, пока пробник ещё жил без
      netns) — живая находка 2026-08-21: на уже развёрнутых нодах эта
      строка переживала апгрейд (мы читаем и переписываем УЖЕ
      установленный файл, см. ``_probe_conf_apply``), и awg-quick
      продолжал НЕ заводить маршрут через сам туннель — curl внутри netns
      уходил бы обычным (не туннелируемым, plaintext) путём через veth,
      что подменяет собой саму суть проверки VPN. Сейчас netns изолирован
      (см. большой комментарий выше), поэтому awg-quick волен управлять
      маршрутизацией ПОЛНОСТЬЮ внутри него как обычно (Table=auto,
      значение по умолчанию) — это ничему не мешает."""
    lines = [
        line
        for line in config_text.splitlines()
        if not line.strip().startswith("DNS ") and not line.strip().startswith("Table")
    ]
    return "\n".join(lines) + "\n"


def _install_probe_conf(conf_path: Path, config_text: str) -> None:
    prepared = _prepare_probe_conf(config_text)
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tmp:
        tmp.write(prepared)
        tmp_path = Path(tmp.name)
    try:
        _sudo(
            [
                "install",
                "-D",
                "-m",
                "0600",
                "-o",
                "root",
                "-g",
                "root",
                str(tmp_path),
                str(conf_path),
            ]
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _probe_conf_path(slot: vpn_probe_state.ProbeSlot) -> Path:
    return VPN_PROBE_AWG_CONF_DIR / f"{slot.iface}.conf"


async def _fetch_probe_config(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> str:
    """Выпросить awg-конфиг пробника у КОНКРЕТНОГО сервера ``slot.server``
    (не «первая живая», как было при единственной ручной паре, — теперь
    пробников несколько, и каждому нужен конфиг именно от своего сервера).
    Тонкий разовый ProtoClient к своей же локальной ноде, та маршрутизирует
    дальше (см. node/peers.py::NodeRouter.route), тем же путём, каким ходит
    nodectl."""
    endpoint = resolve_endpoint(settings.node.socket)
    client = ProtoClient(endpoint, token=settings.swarm.token)
    try:
        await client.connect()
        dst = await vpn_nodes.resolve_vpn_dst(client, server=slot.server)
        if dst is None or dst.node != slot.server:
            raise FixupError(f"vpn@{slot.server} сейчас недоступна — конфиг пробника взять негде")
        result = await client.command(
            vpn_protocol.ACTION_ISSUE,
            {"chat_id": VPN_PROBE_CHAT_ID},
            dst=dst,
            timeout=20.0,
        )
    finally:
        await client.close()
    config_text = result.get("config_text")
    if not config_text:
        raise FixupError(f"vpn@{slot.server} не вернул config_text")
    return str(config_text)


def _probe_conf_check(slot: vpn_probe_state.ProbeSlot) -> bool:
    conf_path = _probe_conf_path(slot)
    if not _privileged_exists(conf_path):
        return False
    try:
        current_conf = _read_privileged(conf_path)
    except FixupError:
        return False
    # Застрявшая строка ``Table`` (обычно ``Table = off`` из старых версий,
    # v0.92.2–v0.92.4) заставляет awg-quick НЕ строить маршрут через
    # туннель — curl из netns уходил бы plaintext мимо VPN, подменяя собой
    # суть проверки (инцидент 2026-08-31). ``_prepare_probe_conf`` её
    # вырезает — но апгрейд с более старой версии её не трогал бы сам.
    return not any(ln.strip().startswith("Table") for ln in current_conf.splitlines())


def _probe_conf_apply(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> None:
    if _which("awg-quick") is None:
        _build_amneziawg_tools()
        if _which("awg-quick") is None:
            raise FixupError("awg-quick не нашёлся после сборки amneziawg-tools")
    conf_path = _probe_conf_path(slot)
    if _privileged_exists(conf_path):
        # Конфиг уже выдан этим сервером раньше — чиним на месте (сносим
        # DNS/Table), не выпрашивая новый пир заново (лишний пир в БД
        # vpn@<сервер> нам не нужен).
        raw_config_text = _read_privileged(conf_path)
    else:
        try:
            raw_config_text = asyncio.run(_fetch_probe_config(settings, slot))
        except Exception as exc:  # noqa: BLE001 — сеть/протокол сведены к одному диагнозу
            raise FixupError(
                f"не удалось получить конфиг у vpn@{slot.server} ({exc}) — "
                "проверьте, что сервер доступен и служба vpn запущена"
            ) from exc
    _install_probe_conf(conf_path, raw_config_text)


def make_vpn_probe_tunnel_conf_fixup(settings: Settings, slot: vpn_probe_state.ProbeSlot) -> Fixup:
    return Fixup(
        id=f"vpn-probe-conf-{slot.server}-{slot.transport}",
        title=f"Получить и установить конфиг awg-пробника к {slot.server}",
        needed=_vpn_check_needed,
        check=lambda: _probe_conf_check(slot),
        apply=lambda: _probe_conf_apply(settings, slot),
    )


def vpn_probe_sudoers_content(
    slots: list[vpn_probe_state.ProbeSlot], ip_path: str, awg_quick_path: str, user: str
) -> str:
    """NOPASSWD ровно на нужные вызовы внутри netns каждого слота (см.
    vpn_check/service.py): ``curl *`` (сами проверки + запрос внешнего IP),
    ``ip route get *`` (гейт «пробник реально в туннеле» перед каждой
    пачкой) и, для awg-слотов, ``awg-quick up/down <iface>`` (сама служба
    поднимает/гасит туннель вокруг чек-цикла — эфемерно, 39.0.7(d)). Не
    голый ``ip`` (равносилен root — умеет менять маршруты/интерфейсы где
    угодно): ``route get`` только читает таблицу маршрутизации. ``curl``
    литералом — он не прямая цель sudo, а аргумент вложенного ``ip netns
    exec``; вложенный ``ip``/``awg-quick`` — резолвленным путём, ровно как
    их зовёт служба. ``awg-quick up/down`` пришпилен к КОНКРЕТНОМУ iface
    слота (не wildcard) — шире не нужно."""
    cmds: list[str] = []
    for slot in slots:
        cmds.append(f"{ip_path} netns exec {slot.netns} curl *")
        cmds.append(f"{ip_path} netns exec {slot.netns} {ip_path} route get *")
        if slot.transport == "awg":
            cmds.append(f"{ip_path} netns exec {slot.netns} {awg_quick_path} up {slot.iface}")
            cmds.append(f"{ip_path} netns exec {slot.netns} {awg_quick_path} down {slot.iface}")
    return f"{user} ALL=(root) NOPASSWD: " + ", ".join(cmds) + "\n"


def _vpn_probe_sudoers_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    path = SUDOERS_DIR / VPN_PROBE_SUDOERS_FILE
    if not _privileged_exists(path):
        return False
    ip_path = _which("ip")
    awg_quick_path = _which("awg-quick")
    if ip_path is None or awg_quick_path is None:
        return False
    # Сверяем содержимое, не только факт существования — старый снипет
    # (другой состав пар) иначе оставлял бы бесполезное/неполное право
    # после изменения состава живых серверов.
    expected = vpn_probe_sudoers_content(slots, ip_path, awg_quick_path, getuser())
    return _read_privileged(path) == expected


def _vpn_probe_sudoers_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    ip_path = _which("ip")
    if ip_path is None:
        raise FixupError("ip (iproute2) не найден в PATH")
    awg_quick_path = _which("awg-quick")
    if awg_quick_path is None:
        raise FixupError("awg-quick не найден — сначала должен пройти vpn-probe-conf-* фикс")
    content = vpn_probe_sudoers_content(slots, ip_path, awg_quick_path, getuser())
    _install_sudoers_snippet(VPN_PROBE_SUDOERS_FILE, content)


def make_vpn_probe_sudoers_fixup(
    settings: Settings, slots: list[vpn_probe_state.ProbeSlot]
) -> Fixup:
    return Fixup(
        id="vpn-check-probe-sudoers",
        title="Разрешить vpn_check ходить в netns пробников и поднимать/гасить awg без пароля",
        needed=_vpn_check_needed,
        check=lambda: _vpn_probe_sudoers_check(slots),
        apply=lambda: _vpn_probe_sudoers_apply(slots),
    )


# --- vpn_check: forward/NAT для veth-подсетей пробников, точечно (НЕ nft -f) ---
#
# vpn_probe_scaffold_unit_content поднимает netns+veth на каждом старте
# юнита (переживает ребут), но САМ ПО СЕБЕ этого недостаточно —
# forward-разрешение и NAT для каждой подсети veth нужно тем же способом,
# каким ноды в этом файле уже чинят firewall (см. «proxy: именованные
# nft-счётчики трафика» ниже) — ТОЧЕЧНО, без единого `nft -f`, чтобы не
# повторить инцидент 2026-08-17 (vpn-jeeves-nat-wiped-by-nftables-reload). В
# отличие от той секции, которая ЗАВЕДОМО правит существующие цепочки
# jeeves (``inet filter``/``ip nat``, известные заранее — needed=vpn_needed,
# только сервер), этот фикс нужен НА ЛЮБОЙ ноде с vpn_check
# (needed=vpn_check_needed) и не может полагаться на конкретные имена
# таблиц — сам находит подходящую существующую forward/postrouting-цепочку
# (см. _find_base_chain), а если такой нет вовсе (типичный случай для ноды
# без своего firewall) — заводит собственную маленькую таблицу с политикой
# accept, которая не меняет поведение остального трафика ноды.


def _nft_json_ruleset() -> list[dict]:
    result = subprocess.run(
        ["sudo", "-n", "nft", "-j", "list", "ruleset"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("nftables", [])


def _find_base_chain(hook: str, chain_type: str) -> tuple[str, str, str] | None:
    """Найти (family, table, chain) первой уже существующей БАЗОВОЙ цепочки
    с данным hook/type — неважно, из какой таблицы (на jeeves это ``inet
    filter``/``ip nat``, заведённые вручную при устранении инцидентов
    2026-08-04/2026-08-17; на других нодах может не быть вообще ничего).
    ``None`` — вызывающий код заведёт свою собственную небольшую таблицу."""
    for item in _nft_json_ruleset():
        chain = item.get("chain")
        if chain is not None and chain.get("hook") == hook and chain.get("type") == chain_type:
            return chain["family"], chain["table"], chain["name"]
    return None


def _nft_ruleset_text() -> str:
    result = subprocess.run(
        ["sudo", "-n", "nft", "list", "ruleset"], capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def _nft_chain_text(family: str, table: str, chain: str) -> str:
    result = subprocess.run(
        ["sudo", "-n", "nft", "list", "chain", family, table, chain],
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


VPN_PROBE_IP_FORWARD_SYSCTL_PATH = Path("/etc/sysctl.d/99-sa-home-vpn-probe-forward.conf")


def _ip_forward_enabled() -> bool:
    try:
        return Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() == "1"
    except OSError:
        return False


def _probe_forwarding_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    if not _ip_forward_enabled():
        return False
    if not slots:
        return True
    text = _nft_ruleset_text()
    return all(slot.veth_host in text and slot.subnet in text for slot in slots)


def _ensure_ip_forward() -> None:
    """``net.ipv4.ip_forward`` — без него ядро дропает пересылаемые пакеты
    ДО netfilter, независимо от того, что говорят nft-правила (живая
    находка 2026-08-21: veth-пара работала — ``ping`` до хоста проходил, а
    до реального интернета — нет, conntrack не показывал вообще ни одной
    записи, даже неудачной). На jeeves он уже был включён ради самого
    VPN-сервера (setup-awg-jeeves.sh), на обычных нодах вроде alfred —
    по умолчанию выключен, здесь включаем его так же явно и одинаково для
    любой ноды с vpn_check."""
    if _ip_forward_enabled():
        return
    if not _privileged_exists(VPN_PROBE_IP_FORWARD_SYSCTL_PATH):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tmp:
            tmp.write("net.ipv4.ip_forward = 1\n")
            tmp_path = Path(tmp.name)
        try:
            _sudo(
                [
                    "install", "-m", "0644", "-o", "root", "-g", "root",
                    str(tmp_path), str(VPN_PROBE_IP_FORWARD_SYSCTL_PATH),
                ]
            )
        finally:
            tmp_path.unlink(missing_ok=True)
    _sudo(["sysctl", "-w", "net.ipv4.ip_forward=1"])


def _ensure_nft_installed() -> None:
    if _which("nft") is not None:
        return
    # Живая находка 2026-08-21: на alfred nftables не установлен вообще
    # (в отличие от jeeves, где он уже был для другого firewall-контура)
    # — без него ЛЮБОЙ `nft ...` тихо проваливается (returncode != 0,
    # ``_nft_json_ruleset``/``_nft_ruleset_text`` трактуют это как
    # «пустой ruleset»), forward/NAT для veth-подсети не появляется, и
    # ничего (ни хендшейк, ни данные) не может выйти из изолированного
    # netns наружу — симптом неотличим от таймаута (curl exit 28).
    argv = install_argv("nftables")
    if argv is None:
        raise FixupError("nft не найден и неизвестен пакетный менеджер для nftables")
    _sudo(argv)


def _resolve_probe_forward_targets() -> tuple[tuple[str, str, str], tuple[str, str, str]]:
    """Найти (или завести собственную) forward/postrouting-цепочку для
    трафика veth-подсетей пробников. Вынесено из ``_probe_forwarding_apply``,
    чтобы ``_vpn_probe_forwarding_persist_apply`` (см. ниже) могла запечь ТЕ
    ЖЕ family/table/chain в скрипт, переживающий ребут, а не изобретать
    решение заново."""
    _ensure_nft_installed()

    forward_target = _find_base_chain("forward", "filter")
    if forward_target is None:
        _sudo(["nft", "add", "table", "inet", VPN_PROBE_NFT_TABLE])
        _sudo(
            [
                "nft", "add", "chain", "inet", VPN_PROBE_NFT_TABLE, "forward",
                "{", "type", "filter", "hook", "forward", "priority", "filter", ";",
                "policy", "accept", ";", "}",
            ]
        )
        forward_target = ("inet", VPN_PROBE_NFT_TABLE, "forward")

    nat_target = _find_base_chain("postrouting", "nat")
    if nat_target is None:
        _sudo(["nft", "add", "table", "ip", VPN_PROBE_NFT_TABLE])
        _sudo(
            [
                "nft", "add", "chain", "ip", VPN_PROBE_NFT_TABLE, "postrouting",
                "{", "type", "nat", "hook", "postrouting", "priority", "srcnat", ";",
                "policy", "accept", ";", "}",
            ]
        )
        nat_target = ("ip", VPN_PROBE_NFT_TABLE, "postrouting")

    return forward_target, nat_target


def _probe_forwarding_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    _ensure_ip_forward()
    if not slots:
        return

    forward_target, nat_target = _resolve_probe_forward_targets()

    ff, ft, fc = forward_target
    forward_text = _nft_chain_text(ff, ft, fc)
    for slot in slots:
        for rule in (f"iifname {slot.veth_host} accept", f"oifname {slot.veth_host} accept"):
            if rule not in forward_text:
                _sudo(["nft", "insert", "rule", ff, ft, fc, *rule.split()])

    nf, nt, nc = nat_target
    nat_text = _nft_chain_text(nf, nt, nc)
    for slot in slots:
        rule = f"ip saddr {slot.subnet} masquerade"
        if rule not in nat_text:
            _sudo(["nft", "insert", "rule", nf, nt, nc, *rule.split()])


def make_vpn_probe_forwarding_fixup(
    settings: Settings, slots: list[vpn_probe_state.ProbeSlot]
) -> Fixup:
    return Fixup(
        id="vpn-check-probe-forwarding",
        title="Пропустить трафик veth-пар пробников наружу (ip_forward + forward + NAT)",
        needed=_vpn_check_needed,
        check=lambda: _probe_forwarding_check(slots),
        apply=lambda: _probe_forwarding_apply(slots),
    )


# --- vpn_check: forward/NAT пробников переживает ребут (systemd, 2026-08-24) ---
#
# Живой инцидент 2026-08-24: alfred потерял питание, после старта
# netns-скаффолд поднялся сам (его юнит переигрывает netns+veth при каждой
# загрузке, см. ``vpn_probe_scaffold_unit_content``), а
# ``vpn-check-probe-forwarding`` — нет: ``nodectl fix`` кладёт nft-правила
# ПРЯМО В ЖИВОЙ ruleset интерактивным sudo и никуда их не сохраняет, а ядро
# ruleset между перезагрузками не хранит. После ребута форвардинг снова
# отсутствовал, помогло только повторное ручное ``nodectl fix``.
#
# Фикс — тот же приём, что уже применён для скаффолда: systemd-юнит,
# который сам (от root, без пароля — не interactive sudo) переигрывает
# нужные nft-правила при каждом старте, для ВСЕХ слотов разом. Family/
# table/chain берутся ИЗ ТОГО ЖЕ ``_resolve_probe_forward_targets()``, каким
# пользуется живой apply() — на jeeves это переиспользование уже
# существующих цепочек firewall, на alfred — собственная маленькая таблица
# ``VPN_PROBE_NFT_TABLE``. Идемпотентность каждого шага — через ``grep -qF``
# по ``nft list`` (голый повторный ``nft insert rule`` иначе плодил бы
# дубликаты при каждом рестарте юнита, не только при чистом буте).

VPN_PROBE_FORWARD_SCRIPT_PATH = Path("/usr/local/lib/sa-home-bot/vpn-probe-forward-reapply.sh")
VPN_PROBE_FORWARD_UNIT_FILE = Path("/etc/systemd/system/sa-home-vpn-probe-forward.service")


def _vpn_probe_forward_script_content(
    forward_target: tuple[str, str, str],
    nat_target: tuple[str, str, str],
    slots: list[vpn_probe_state.ProbeSlot],
) -> str:
    ff, ft, fc = forward_target
    nf, nt, nc = nat_target
    lines = [
        "#!/bin/sh",
        "# sa-home-bot: сгенерировано nodectl fix — не редактировать руками,",
        "# перезапишется при следующем nodectl fix.",
        "set -e",
    ]

    def ensure_chain(family: str, table: str, chain: str, chain_def: str) -> None:
        # Свою таблицу могли не найти после чистого бута (она тоже не
        # переживает ребут) — досоздаём её тем же способом, каким её завёл
        # бы ``_resolve_probe_forward_targets()``. Чужие таблицы (найденные
        # уже существующими на момент ``nodectl fix``, напр. firewall
        # jeeves) не трогаем — их персистентность не в ведении этого фикса.
        if table != VPN_PROBE_NFT_TABLE:
            return
        lines.append(
            f"nft list table {family} {table} >/dev/null 2>&1 || {{ "
            f"nft add table {family} {table}; "
            f"nft add chain {family} {table} {chain} '{chain_def}'; }}"
        )

    ensure_chain(ff, ft, fc, "{ type filter hook forward priority filter ; policy accept ; }")
    ensure_chain(nf, nt, nc, "{ type nat hook postrouting priority srcnat ; policy accept ; }")

    def ensure_rule(family: str, table: str, chain: str, match: str, rule: str) -> None:
        lines.append(
            f"nft list chain {family} {table} {chain} 2>/dev/null | grep -qF '{match}' || "
            f"nft insert rule {family} {table} {chain} {rule}"
        )

    for slot in slots:
        ensure_rule(
            ff, ft, fc,
            f'iifname "{slot.veth_host}" accept',
            f"iifname {slot.veth_host} accept",
        )
        ensure_rule(
            ff, ft, fc,
            f'oifname "{slot.veth_host}" accept',
            f"oifname {slot.veth_host} accept",
        )
        ensure_rule(
            nf, nt, nc,
            f"ip saddr {slot.subnet} masquerade",
            f"ip saddr {slot.subnet} masquerade",
        )

    return "\n".join(lines) + "\n"


def vpn_probe_forward_unit_content(
    script_path: Path, slots: list[vpn_probe_state.ProbeSlot]
) -> str:
    scaffold_units = " ".join(_probe_scaffold_unit_path(s).name for s in slots)
    after = "network-online.target" + (f" {scaffold_units}" if scaffold_units else "")
    return (
        "[Unit]\n"
        "Description=sa-home-bot: forward/NAT для veth-пар VPN-пробников "
        "(переживает ребут)\n"
        f"After={after}\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart=/bin/sh {script_path}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def _vpn_probe_forwarding_persist_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    if not (
        _privileged_exists(VPN_PROBE_FORWARD_SCRIPT_PATH)
        and _privileged_exists(VPN_PROBE_FORWARD_UNIT_FILE)
    ):
        return False
    # Сверяем содержимое юнита, не только факт существования файла — живой
    # апгрейд 2026-08-17 (старый юнит гонял awg-quick с другим ExecStart)
    # иначе застрял бы со старым содержимым навсегда.
    expected_unit = vpn_probe_forward_unit_content(VPN_PROBE_FORWARD_SCRIPT_PATH, slots)
    try:
        current_unit = _read_privileged(VPN_PROBE_FORWARD_UNIT_FILE)
    except FixupError:
        return False
    if current_unit != expected_unit:
        return False
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", VPN_PROBE_FORWARD_UNIT_FILE.name]
        ).returncode
        == 0
    )


def _vpn_probe_forwarding_persist_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    _ensure_ip_forward()
    forward_target, nat_target = _resolve_probe_forward_targets()
    script_content = _vpn_probe_forward_script_content(forward_target, nat_target, slots)

    current_script = (
        _read_privileged(VPN_PROBE_FORWARD_SCRIPT_PATH)
        if _privileged_exists(VPN_PROBE_FORWARD_SCRIPT_PATH)
        else None
    )
    if current_script != script_content:
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as tmp:
            tmp.write(script_content)
            tmp_path = Path(tmp.name)
        try:
            _sudo(["install", "-D", "-m", "0755", "-o", "root", "-g", "root",
                   str(tmp_path), str(VPN_PROBE_FORWARD_SCRIPT_PATH)])
        finally:
            tmp_path.unlink(missing_ok=True)

    unit_content = vpn_probe_forward_unit_content(VPN_PROBE_FORWARD_SCRIPT_PATH, slots)
    current_unit = (
        _read_privileged(VPN_PROBE_FORWARD_UNIT_FILE)
        if _privileged_exists(VPN_PROBE_FORWARD_UNIT_FILE)
        else None
    )
    if current_unit != unit_content:
        if current_unit is not None:
            _sudo(["systemctl", "stop", VPN_PROBE_FORWARD_UNIT_FILE.name])
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as tmp:
            tmp.write(unit_content)
            tmp_path = Path(tmp.name)
        try:
            _sudo(["install", "-m", "0644", "-o", "root", "-g", "root",
                   str(tmp_path), str(VPN_PROBE_FORWARD_UNIT_FILE)])
        finally:
            tmp_path.unlink(missing_ok=True)
        _sudo(["systemctl", "daemon-reload"])
    _sudo(["systemctl", "enable", "--now", VPN_PROBE_FORWARD_UNIT_FILE.name])


def make_vpn_probe_forwarding_persist_fixup(
    settings: Settings, slots: list[vpn_probe_state.ProbeSlot]
) -> Fixup:
    return Fixup(
        id="vpn-check-probe-forwarding-persist",
        title="Пережить перезагрузку: nft forward/NAT пробников переигрываются systemd-юнитом",
        needed=_vpn_check_needed,
        check=lambda: _vpn_probe_forwarding_persist_check(slots),
        apply=lambda: _vpn_probe_forwarding_persist_apply(slots),
    )


# --- vpn_check: состояние пробника — что реально настроено, fixup пишет,
# служба читает (39.0.7(d)) ---
#
# До 39.0.7(d) единственная пара (сервер, транспорт) жила в
# ``[vpn_check] probe_server``/``probe_transport`` config.toml, правилась
# вручную. Список целей теперь автообнаруживается и может меняться без
# участия человека — состояние, а не конфиг (см. докстринг
# node/vpn_probe_state.py). Несёт ВСЕ обнаруженные пары, включая
# транспорты, для которых scaffold/conf/sudoers ещё не заведены (reality —
# до 39.0.7(e)) — vpn_check/service.py уже умеет отвечать на них мягкой
# ошибкой «транспорт пока не поддержан», а не молчаливым пропуском, так что
# после 39.0.7(e) они заработают без изменения формата файла.


def _vpn_probe_state_check(slots: list[vpn_probe_state.ProbeSlot]) -> bool:
    path = vpn_probe_state.STATE_PATH
    if not _privileged_exists(path):
        return False
    try:
        current = _read_privileged(path)
    except FixupError:
        return False
    return current == vpn_probe_state.render(slots)


def _vpn_probe_state_apply(slots: list[vpn_probe_state.ProbeSlot]) -> None:
    content = vpn_probe_state.render(slots)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        _sudo(
            [
                "install", "-D", "-m", "0644", "-o", "root", "-g", "root",
                str(tmp_path), str(vpn_probe_state.STATE_PATH),
            ]
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def make_vpn_probe_state_fixup(settings: Settings, slots: list[vpn_probe_state.ProbeSlot]) -> Fixup:
    return Fixup(
        id="vpn-check-probe-state",
        title="Записать список настроенных пробников (что реально проверяется с этой ноды)",
        needed=_vpn_check_needed,
        check=lambda: _vpn_probe_state_check(slots),
        apply=lambda: _vpn_probe_state_apply(slots),
    )


# --- proxy: mtg (MTProto) + microsocks (SOCKS5) на jeeves, 2026-08-17 ---
#
# Кодифицирует то, что раньше было поднято вручную по SSH 2026-08-13 (см.
# память telegram-bot-api-proxy-2026-08-13) — ссылку/секрет/учёт трафика
# отдаёт бот через службу vpn (vpn/service.py::_proxy_link и т.д.), здесь
# только воспроизводимая установка демонов и firewall. needed — тот же
# предикат, что у awg-фиксапов (только jeeves), отдельного флага не заводим.

_MTG_VERSION = "2.2.8"
_MTG_SHA256 = "7ef19d079d85f4e00d4f8334ec1f3f3c8718e3d0ed1f3109ea9a8673138a2102"
_MTG_URL = (
    f"https://github.com/9seconds/mtg/releases/download/v{_MTG_VERSION}/"
    f"mtg-{_MTG_VERSION}-linux-amd64.tar.gz"
)
MTG_BIN_PATH = Path("/usr/local/bin/mtg")
PROXY_UNITS_DIR = Path.home() / ".config" / "systemd" / "user"
MTG_UNIT_PATH = PROXY_UNITS_DIR / "mtg.service"
MICROSOCKS_UNIT_PATH = PROXY_UNITS_DIR / "microsocks.service"
PROXY_FIREWALL_SUDOERS_FILE = "50-sa-home-node-proxy-nft"
NFTABLES_CONF_PATH = Path("/etc/nftables.conf")


def mtg_unit_content(port: int, secret: str) -> str:
    return (
        "[Unit]\n"
        "Description=mtg (MTProto proxy for Telegram)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={MTG_BIN_PATH} simple-run 0.0.0.0:{port} {secret}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def microsocks_unit_content(bind_host: str, port: int, microsocks_path: str) -> str:
    return (
        "[Unit]\n"
        "Description=microsocks (SOCKS5 proxy for sa-home-bot Telegram egress)\n"
        "After=network-online.target tailscaled.service\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={microsocks_path} -i {bind_host} -p {port}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemctl_user_is_active(unit: str) -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0


def _proxy_units_check() -> bool:
    return (
        MTG_BIN_PATH.is_file()
        and MTG_UNIT_PATH.exists()
        and MICROSOCKS_UNIT_PATH.exists()
        and _systemctl_user_is_active("mtg.service")
        and _systemctl_user_is_active("microsocks.service")
    )


def _install_mtg() -> None:
    if MTG_BIN_PATH.is_file():
        return
    with tempfile.TemporaryDirectory(prefix="sa-home-mtg-") as build_dir_str:
        build_dir = Path(build_dir_str)
        tarball = build_dir / "mtg.tar.gz"
        _run(["curl", "-fsSL", _MTG_URL, "-o", str(tarball)], timeout=60)
        digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if digest != _MTG_SHA256:
            raise FixupError(
                f"mtg-{_MTG_VERSION}-linux-amd64.tar.gz: sha256 не совпал "
                f"(ждали {_MTG_SHA256}, получили {digest}) — установка прервана"
            )
        _run(["tar", "-C", str(build_dir), "-xzf", str(tarball)])
        extracted = build_dir / f"mtg-{_MTG_VERSION}-linux-amd64" / "mtg"
        if not extracted.is_file():
            raise FixupError(f"после распаковки не нашёлся бинарь mtg ({extracted})")
        _sudo(["install", "-m", "0755", str(extracted), str(MTG_BIN_PATH)])
    setcap = _which("setcap")
    if setcap is None:
        raise FixupError("setcap не найден (пакет libcap2-bin)")
    _sudo([setcap, "cap_net_bind_service=+ep", str(MTG_BIN_PATH)])


def _install_microsocks() -> None:
    if _which("microsocks") is not None:
        return
    argv = install_argv("microsocks")
    if argv is None:
        raise FixupError("не найден известный пакетный менеджер для microsocks")
    _sudo(argv)


def _proxy_units_apply(settings: Settings) -> None:
    _install_mtg()
    _install_microsocks()
    if not settings.vpn.socks_host:
        raise FixupError(
            "не настроен [vpn].socks_host — впишите tailscale-адрес jeeves и повторите nodectl fix"
        )
    PROXY_UNITS_DIR.mkdir(parents=True, exist_ok=True)
    if not MTG_UNIT_PATH.exists():
        # Секрет — PROXY_SECRET_SEED (см. vpn/protocol.py): тот же литерал,
        # которым vpn/service.py::_proxy_secret сидирует БД при первом
        # старте — оба места сходятся без похода друг к другу. Дальнейшая
        # смена — только через proxy_rotate_secret (правит и БД, и юнит).
        MTG_UNIT_PATH.write_text(
            mtg_unit_content(settings.vpn.mtg_port, vpn_protocol.PROXY_SECRET_SEED),
            encoding="utf-8",
        )
    if not MICROSOCKS_UNIT_PATH.exists():
        microsocks_path = _which("microsocks") or "/usr/bin/microsocks"
        MICROSOCKS_UNIT_PATH.write_text(
            microsocks_unit_content(
                settings.vpn.socks_host, settings.vpn.socks_port, microsocks_path
            ),
            encoding="utf-8",
        )
    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", "mtg.service", "microsocks.service"])


def make_proxy_units_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="proxy-units",
        title="Поднять mtg (MTProto) + microsocks (SOCKS5) на jeeves",
        needed=_vpn_awg_needed,
        check=_proxy_units_check,
        apply=lambda: _proxy_units_apply(settings),
    )


# --- proxy: именованные nft-счётчики трафика, точечно (НЕ nft -f) ---
#
# Живой инцидент 2026-08-17 (vpn-jeeves-nat-wiped-by-nftables-reload):
# полный `nft -f /etc/nftables.conf` стирает ВСЕ таблицы, включая
# iptables-nft таблицы, которые awg-quick добавляет себе через PostUp —
# NAT/FORWARD у AmneziaWG пропадают без единой ошибки, VPN хендшейкается,
# но не даёт интернета. Здесь — ТОЛЬКО точечные `nft add`/`insert`.


def _nft_output(argv: list[str]) -> str | None:
    result = subprocess.run(["sudo", "-n", "nft", *argv], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def _filter_table_json() -> dict:
    output = _nft_output(["-j", "list", "table", "inet", "filter"])
    if output is None:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {}


def _existing_counter_names(data: dict) -> set[str]:
    """Именованные счётчики, СОЗДАННЫЕ (``nft add counter``) — не значит, что
    на них ссылается хоть одно правило. См. ``_rule_counter_names`` для
    проверки того, что реально считает трафик."""
    return {
        counter["name"]
        for item in data.get("nftables", [])
        if (counter := item.get("counter")) is not None
    }


def _rule_counter_names(data: dict) -> set[str]:
    """Именованные счётчики, на которые ссылается хотя бы одно ПРАВИЛО в
    chain input — только это реально считает трафик. Живой баг 2026-08-17
    (найден 2026-08-21): ``_proxy_firewall_check`` раньше проверял только
    существование счётчика-объекта (``nft add counter``) и после его
    создания считал фикс уже применённым, из-за чего строки ``nft insert
    rule ...`` из ``_apply_proxy_firewall_live`` ниже никогда не
    выполнялись — счётчики существовали, но ни разу не увеличивались
    (`bytes: 0` навсегда), а `check()` при этом врал, что всё готово."""
    names: set[str] = set()
    for item in data.get("nftables", []):
        rule = item.get("rule")
        if rule is None or rule.get("chain") != "input":
            continue
        for expr in rule.get("expr", []):
            counter = expr.get("counter")
            # Ссылка на ИМЕНОВАННЫЙ счётчик в expr правила — голая строка
            # (`{"counter": "mtg_bytes"}`), не объект с "name" (тот вид —
            # только у самого ОПРЕДЕЛЕНИЯ счётчика, отдельным top-level
            # элементом "nftables"). Проверено эмпирически на jeeves
            # 2026-08-21 в изолированной scratch-таблице — реальный `nft -j`
            # так и вернул, объектную форму ждали ошибочно.
            if isinstance(counter, str):
                names.add(counter)
            elif isinstance(counter, dict) and counter.get("name"):
                names.add(counter["name"])
    return names


def _proxy_firewall_check() -> bool:
    return {"mtg_bytes", "socks_bytes"} <= _rule_counter_names(_filter_table_json())


def _nft_check_file(path: Path) -> None:
    """`nft -c -f` — разбор ruleset БЕЗ применения к ядру. Ловит ровно тот
    класс поломки, что положил /etc/nftables.conf в инциденте 2026-08-31:
    counter-ОБЪЕКТ объявили внутри `chain` (а не на уровне таблицы) и
    правило вставили ДО строки `type ... hook ...` — глазами читается,
    `nft -f` при следующем ребуте падает, nftables.service не стартует,
    VPN остаётся без NAT. Интерактивный sudo (как и остальной apply());
    ошибки nft печатает прямо в терминал."""
    nft = _which("nft") or "nft"
    try:
        result = subprocess.run(["sudo", nft, "-c", "-f", str(path)])
    except OSError as exc:
        raise FixupError(f"не удалось запустить nft -c: {exc}") from exc
    if result.returncode != 0:
        raise FixupError(
            f"сгенерированный {NFTABLES_CONF_PATH} не проходит `nft -c` "
            "(ошибку см. выше) — файл НЕ изменён"
        )


def _append_nftables_conf_counters(settings: Settings) -> None:
    """Дописать счётчики в ПЕРСИСТЕНТНЫЙ /etc/nftables.conf — этот файл
    применяется только при бутстрапе systemd (следующий чистый бут), сам
    файл переписать безопасно; недопустимо только ПРИМЕНЯТЬ его целиком на
    живой ноде (`nft -f`/`systemctl restart nftables`) — этого здесь нет и
    не будет, см. предупреждение выше.

    Именованные counter-ОБЪЕКТЫ объявляются на уровне ТАБЛИЦЫ (не внутри
    chain), а строка `type ... hook ... priority ...` обязана быть первой
    в цепочке — правила со счётчиками вставляем ПОСЛЕ неё. Нарушение того
    и другого сломало конфиг в инциденте 2026-08-31 (`nft -f` падал на
    следующем ребуте). Результат прогоняем через `nft -c` ДО install —
    кривой файл в /etc/nftables.conf не попадёт."""
    result = subprocess.run(
        ["sudo", "cat", str(NFTABLES_CONF_PATH)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise FixupError(f"не удалось прочитать {NFTABLES_CONF_PATH}: {result.stderr.strip()}")
    content = result.stdout
    if "counter mtg_bytes" in content:
        return

    lines = content.splitlines(keepends=True)

    def _find(pred: Callable[[str], bool], start: int = 0) -> int | None:
        return next((i for i in range(start, len(lines)) if pred(lines[i])), None)

    def _indent(line: str) -> str:
        return line[: len(line) - len(line.lstrip())]

    table_i = _find(lambda s: s.strip() == "table inet filter {")
    chain_i = (
        _find(lambda s: s.strip() == "chain input {", table_i + 1)
        if table_i is not None
        else None
    )
    hook_i = (
        _find(lambda s: s.lstrip().startswith("type filter hook input"), chain_i + 1)
        if chain_i is not None
        else None
    )
    if table_i is None or chain_i is None or hook_i is None:
        raise FixupError(
            f"{NFTABLES_CONF_PATH}: не нашёл 'table inet filter {{' / 'chain input {{' / "
            "строку 'type filter hook input' — структура файла не та, что ожидалась; "
            "допишите счётчики вручную (см. node/fixups.py::_append_nftables_conf_counters) "
            "и повторите nodectl fix"
        )

    obj_indent = _indent(lines[chain_i])
    rule_indent = _indent(lines[hook_i])
    counter_objs = (
        f"{obj_indent}counter mtg_bytes {{ }}\n"
        f"{obj_indent}counter socks_bytes {{ }}\n"
    )
    counter_rules = (
        f"{rule_indent}tcp dport {settings.vpn.mtg_port} counter name mtg_bytes accept\n"
        f'{rule_indent}iifname "tailscale0" tcp dport {settings.vpn.socks_port} '
        "counter name socks_bytes accept\n"
    )
    # Сначала нижняя вставка (hook_i), потом верхняя (table_i) — иначе
    # индексы разъезжаются.
    lines.insert(hook_i + 1, counter_rules)
    lines.insert(table_i + 1, counter_objs)
    new_content = "".join(lines)

    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tmp:
        tmp.write(new_content)
        tmp_path = Path(tmp.name)
    try:
        _nft_check_file(tmp_path)
        _sudo(
            [
                "install", "-m", "0755", "-o", "root", "-g", "root",
                str(tmp_path), str(NFTABLES_CONF_PATH),
            ]
        )
    finally:
        tmp_path.unlink(missing_ok=True)


def _apply_proxy_firewall_live(settings: Settings) -> None:
    """Точечно в живой ruleset. ``nft insert rule`` БЕЗ явной позиции
    добавляет правило В НАЧАЛО цепочки (проверено эмпирически на jeeves
    2026-08-17 в изолированной тестовой таблице) — новое правило со
    счётчиком получает пакет первым и отрабатывает раньше старого голого
    ``tcp dport 443 accept``/блочного ``iifname "tailscale0" accept``.
    Старые правила НЕ трогаем и не удаляем — они просто становятся
    недостижимы для этих портов, что безопаснее, чем trying to delete by
    handle."""
    data = _filter_table_json()
    existing_counters = _existing_counter_names(data)
    if "mtg_bytes" not in existing_counters:
        _sudo(["nft", "add", "counter", "inet", "filter", "mtg_bytes"])
    if "socks_bytes" not in existing_counters:
        _sudo(["nft", "add", "counter", "inet", "filter", "socks_bytes"])
    # Проверяем именно ПРАВИЛА (не факт существования счётчиков — см.
    # докстринг _rule_counter_names про баг 2026-08-17/2026-08-21), иначе
    # только что созданные строкой выше счётчики без единого правила на
    # них дадут ложное «уже готово» и insert ниже никогда не выполнится.
    if {"mtg_bytes", "socks_bytes"} <= _rule_counter_names(data):
        return
    _sudo(
        [
            "nft", "insert", "rule", "inet", "filter", "input",
            "tcp", "dport", str(settings.vpn.mtg_port),
            "counter", "name", "mtg_bytes", "accept",
        ]
    )
    _sudo(
        [
            "nft", "insert", "rule", "inet", "filter", "input",
            "iifname", "tailscale0", "tcp", "dport", str(settings.vpn.socks_port),
            "counter", "name", "socks_bytes", "accept",
        ]
    )


def proxy_firewall_sudoers_content(nft_path: str, user: str) -> str:
    return (
        f"{user} ALL=(root) NOPASSWD: {nft_path} -j list counter inet filter mtg_bytes, "
        f"{nft_path} -j list counter inet filter socks_bytes\n"
    )


def _proxy_firewall_apply(settings: Settings) -> None:
    _append_nftables_conf_counters(settings)
    _apply_proxy_firewall_live(settings)
    nft_path = _which("nft") or "/usr/sbin/nft"
    _install_sudoers_snippet(
        PROXY_FIREWALL_SUDOERS_FILE, proxy_firewall_sudoers_content(nft_path, getuser())
    )


def make_proxy_firewall_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="proxy-firewall",
        title="Счётчики трафика прокси в firewall (точечно, без полного reload)",
        needed=_vpn_awg_needed,
        check=_proxy_firewall_check,
        apply=lambda: _proxy_firewall_apply(settings),
    )


def build_fixups(settings: Settings) -> list[Fixup]:
    """Известные фиксы, актуальные для текущих назначений ноды (``needed``).

    Пробники VPN (39.0.7(d)) — единственные фиксы, чей СОСТАВ (не только
    применимость) зависит от роя: список (сервер, транспорт) обнаруживается
    один раз здесь (``_discover_probe_slots``), и на каждую awg-пару
    заводится СВОЙ набор фиксов (scaffold+conf), по образцу
    ``*(make_apps_unit_fixup(app) for app in settings.apps.items)`` ниже —
    явной «фабрики-списка» в этом файле не было, только этот паттерн."""
    fixups = [
        INSTALL_SMARTMONTOOLS,
        SMARTCTL_SUDOERS,
        NODE_UNIT_SMARTCTL_PATH,
        JOURNALCTL_GROUP,
        POWER_CONTROL_POLKIT,
        WOL_ENABLE,
        make_awg_sudoers_fixup(settings),
    ]
    if _vpn_check_needed(settings):
        slots = _discover_probe_slots(settings)
        # Reality пока не умеет ни scaffold, ни sudoers, ни conf-фикс
        # (39.0.7(e)) — их слоты остаются только в vpn-check-probe-state,
        # см. докстринг той секции.
        awg_slots = [s for s in slots if s.transport == "awg"]
        fixups += [
            make_vpn_probe_forwarding_fixup(settings, awg_slots),
            make_vpn_probe_forwarding_persist_fixup(settings, awg_slots),
            *(make_vpn_probe_scaffold_fixup(settings, slot) for slot in awg_slots),
            *(make_vpn_probe_tunnel_conf_fixup(settings, slot) for slot in awg_slots),
            make_vpn_probe_sudoers_fixup(settings, awg_slots),
            make_vpn_probe_state_fixup(settings, slots),
        ]
    fixups += [
        make_proxy_units_fixup(settings),
        make_proxy_firewall_fixup(settings),
        *(make_apps_unit_fixup(app) for app in settings.apps.items),
    ]
    return [f for f in fixups if f.needed(settings)]


def _check_or_false(fixup: Fixup) -> bool:
    """``fixup.check()``, терпимый к тому, что сама проверка не может
    исполниться (напр. ``_read_privileged`` внутри check() упёрлась в sudo
    без TTY/кэша — живой краш 2026-08-17: check() кидал FixupError,
    run_fixups его не ловил и падал целиком, останавливая ВСЕ оставшиеся
    фиксы, не только этот один). Раз проверка не смогла подтвердить «уже
    применено» — считаем, что не применено, и пробуем apply()."""
    try:
        return fixup.check()
    except FixupError as exc:
        print(f"  ⚠️ {fixup.title}: проверка не удалась ({exc}) — пробую применить")
        return False


def run_fixups(fixups: list[Fixup]) -> list[str]:
    """Применить фиксы по одному (идемпотентно), печатая прогресс.

    Общий код между ``nodectl fix`` и ``sa-home-bot init`` (который зовёт
    фиксы сам сразу после первой установки workstation-ноды). Возвращает id
    тех, что не удалось применить/подтвердить.
    """
    failed: list[str] = []
    for fixup in fixups:
        if _check_or_false(fixup):
            print(f"  ✅ {fixup.title} — уже применено")
            continue
        print(f"  ⏳ {fixup.title} — применяю (может спросить пароль sudo)...")
        try:
            fixup.apply()
        except FixupError as exc:
            print(f"  ❌ {fixup.title}: {exc}")
            failed.append(fixup.id)
            continue
        if _check_or_false(fixup):
            print(f"  ✅ {fixup.title} — применено")
        else:
            print(f"  ⚠️ {fixup.title}: команда прошла, но проверка всё ещё отрицательна")
            failed.append(fixup.id)
    return failed
