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
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path

from sa_home_bot import wol
from sa_home_bot.config import AppConfig, Settings
from sa_home_bot.node import assignments
from sa_home_bot.node import kind as node_kinds
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import resolve_endpoint
from sa_home_bot.proto.messages import Address
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
        needed=_vpn_needed,
        check=_awg_sudoers_check,
        apply=lambda: _awg_sudoers_apply(settings),
    )


# --- vpn_check: клиентский туннель-пробник (awg-quick, Table=off) ---
#
# Служба vpn_check (vpn_check/service.py) сама awg-quick не поднимает и root
# не получает (тот же инвариант «долгоживущий процесс сам sudo не зовёт», что
# у остальных служб) — готовый туннель ей нужен заранее, поднятый этим
# фиксом. Конфиг (приватный ключ) получаем ровно тем же способом, каким его
# получил бы обычный гость: зовём action "issue" службы vpn на jeeves с
# chat_id=0 (см. vpn/service.py::NODE_SENTINEL_CHAT_ID — уже зарезервирован
# под "не гостя, а саму ноду"; отдельного "системного" действия в VpnService
# заводить не понадобилось, решение пользователя 2026-08-17: `_issue()` не
# проверяет принадлежность chat_id реальному чату, это просто ключ учёта в
# БД).
#
# Живая находка 2026-08-17: интерфейс сначала жил в отдельном network
# namespace (``ip netns add``) ради изоляции от основной маршрутизации —
# но у такого netns нет НИ ОДНОГО физического интерфейса, поэтому самому
# WireGuard-хендшейку (обычный, не туннелируемый UDP до эндпоинта jeeves)
# было решительно некуда уйти в интернет: `ip route show` внутри netns был
# пуст, `awg show` не показывал ни одного хендшейка, все проверки висели по
# таймауту (curl exit 28) на обеих нодах разом. Правильно чинить это можно
# было бы veth-парой + NAT в root netns, но это лишний ход в firewall двух
# продакшен-машин (решение пользователя 2026-08-17: не стоит той цены).
#
# Вместо netns — `Table = off` в конфиге пробника (см. _prepare_probe_conf):
# awg-quick поднимает интерфейс и адрес, но НЕ трогает основную
# маршрутизацию хоста вообще (ни фейкового default route, ни fwmark/ip
# rule — обычный трафик ноды идёт как шёл). Свой маршрут добавляем сами,
# ExecStartPost в vpn_probe_unit_content, с высоким metric — обычный
# трафик хоста его никогда не выберет. Единственный, кто им пользуется —
# curl самого пробника, явно пришпиленный к интерфейсу через
# `--interface awg-probe0` (SO_BINDTODEVICE, vpn_check/service.py), которому
# для этого route нужен ХОТЬ КАКОЙ-то, пусть и с огромным metric.

VPN_PROBE_IFACE = "awg-probe0"
VPN_PROBE_CONF_DIR = Path("/etc/amnezia/amneziawg")
VPN_PROBE_UNIT_FILE = Path("/etc/systemd/system/sa-home-vpn-probe.service")
VPN_PROBE_SUDOERS_FILE = "50-sa-home-node-vpn-probe"
# vpn/service.py::NODE_SENTINEL_CHAT_ID — не импортируем службу целиком
# (тяжёлые зависимости: БД, awg-бэкенд) ради одной константы, тот же приём,
# что уже используют node/peers.py и другие (свой NODE_SERVICE = "node").
VPN_PROBE_CHAT_ID = 0


def _vpn_check_needed(settings: Settings) -> bool:
    return assignments.has_service(settings.node.assignments, "vpn_check")


def _vpn_probe_conf_path(settings: Settings) -> Path:
    return VPN_PROBE_CONF_DIR / f"{VPN_PROBE_IFACE}.conf"


def vpn_probe_unit_content(iface: str, ip_path: str, awg_quick_path: str) -> str:
    """systemd-юнит: awg-quick поднимает интерфейс в обычном (root) netns —
    ``Table = off`` в самом конфиге (см. ``_prepare_probe_conf``) не даёт ему
    тронуть основную маршрутизацию хоста. ``ExecStartPost`` добавляет
    единственный маршрут, которым пользуется только curl пробника, явно
    пришпиленный к интерфейсу (``--interface``, см. vpn_check/service.py) —
    высокий metric гарантирует, что обычный трафик хоста его не подхватит."""
    return (
        "[Unit]\n"
        "Description=sa-home-bot: VPN-пробник для мониторинга доступности "
        f"({iface})\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "RemainAfterExit=yes\n"
        f"ExecStart={awg_quick_path} up {iface}\n"
        f"ExecStartPost={ip_path} route add default dev {iface} metric 10000\n"
        f"ExecStop={awg_quick_path} down {iface}\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def vpn_probe_sudoers_content(curl_path: str, iface: str, user: str) -> str:
    """NOPASSWD только на ``curl --interface awg-probe0 *`` (единственный
    вызов рантайма, см. vpn_check/service.py) — ``--interface`` (не голый
    ``curl *``) не даёт этим правом дотянуться до чего-то за пределами
    трафика пробника; SO_BINDTODEVICE, которым занимается ``--interface``,
    и есть причина, почему вызов вообще требует root."""
    return f"{user} ALL=(root) NOPASSWD: {curl_path} --interface {iface} *\n"


async def _fetch_probe_config(settings: Settings) -> str:
    """Выпросить конфиг пробника у vpn@jeeves — тонкий разовый ProtoClient к
    своей же локальной ноде, та маршрутизирует дальше (см.
    node/peers.py::NodeRouter.route), тем же путём, каким ходит nodectl."""
    endpoint = resolve_endpoint(settings.node.socket)
    client = ProtoClient(endpoint, token=settings.swarm.token)
    try:
        await client.connect()
        result = await client.command(
            vpn_protocol.ACTION_ISSUE,
            {"chat_id": VPN_PROBE_CHAT_ID},
            dst=Address(node=vpn_protocol.NODE_ID, service=vpn_protocol.SERVICE_NAME),
            timeout=20.0,
        )
    finally:
        await client.close()
    config_text = result.get("config_text")
    if not config_text:
        raise FixupError("vpn@jeeves не вернул config_text")
    return str(config_text)


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
    """Две правки конфига, выданного vpn@jeeves как обычному гостю:

    - без строки ``DNS =`` — awg-quick иначе сам зовёт ``resolvconf``,
      которого нет на Debian, и весь скрипт падает (живая находка
      2026-08-17). DNS пробнику отдельно решать не нужно: интерфейс живёт в
      обычном (root) netns, где host resolv.conf и так работает — см.
      ``vpn_check/service.py`` (curl резолвит имя как обычно, только сама
      TCP/TLS-сессия пришпилена к интерфейсу через ``--interface``).
    - добавляет ``Table = off``, если такой строки ещё нет — без неё
      awg-quick попробует стать основным default route хоста (см. большой
      комментарий выше про netns/veth)."""
    lines = [line for line in config_text.splitlines() if not line.strip().startswith("DNS ")]
    if not any(line.strip().startswith("Table") for line in lines):
        try:
            insert_at = lines.index("[Interface]") + 1
        except ValueError:
            insert_at = 0
        lines.insert(insert_at, "Table = off")
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


def _vpn_probe_tunnel_check(settings: Settings) -> bool:
    if not (
        _privileged_exists(_vpn_probe_conf_path(settings))
        and _privileged_exists(VPN_PROBE_UNIT_FILE)
    ):
        return False
    # Сверяем содержимое юнита, не только факт его существования — живой
    # застрявший апгрейд 2026-08-17: старый netns-юнит был технически
    # "active (exited)" (успешно стартовал когда-то со старым содержимым),
    # проверка по одному только is-active сочла бы его «уже применённым» и
    # apply() с новым содержимым не вызвался бы вовсе.
    awg_quick_path = _which("awg-quick")
    ip_path = _which("ip")
    if awg_quick_path is None or ip_path is None:
        return False
    expected_unit = vpn_probe_unit_content(VPN_PROBE_IFACE, ip_path, awg_quick_path)
    try:
        current_unit = _read_privileged(VPN_PROBE_UNIT_FILE)
    except FixupError:
        return False
    if current_unit != expected_unit:
        return False
    return (
        subprocess.run(["systemctl", "is-active", "--quiet", VPN_PROBE_UNIT_FILE.name]).returncode
        == 0
    )


def _vpn_probe_tunnel_apply(settings: Settings) -> None:
    awg_quick_path = _which("awg-quick")
    if awg_quick_path is None:
        _build_amneziawg_tools()
        awg_quick_path = _which("awg-quick")
        if awg_quick_path is None:
            raise FixupError("awg-quick не нашёлся после сборки amneziawg-tools")
    ip_path = _which("ip")
    if ip_path is None:
        raise FixupError("ip (iproute2) не найден в PATH")

    conf_path = _vpn_probe_conf_path(settings)
    if _privileged_exists(conf_path):
        # Конфиг уже выдан jeeves раньше — чиним на месте (сносим DNS,
        # добавляем Table=off), не выпрашивая новый пир заново (лишний пир в
        # БД vpn@jeeves нам не нужен).
        raw_config_text = _read_privileged(conf_path)
    else:
        try:
            raw_config_text = asyncio.run(_fetch_probe_config(settings))
        except Exception as exc:  # noqa: BLE001 — сеть/протокол сведены к одному диагнозу
            raise FixupError(
                f"не удалось получить конфиг у vpn@jeeves ({exc}) — "
                "проверьте, что jeeves доступен и служба vpn запущена"
            ) from exc
    _install_probe_conf(conf_path, raw_config_text)

    # Всегда сверяем содержимое, не только факт существования файла — живой
    # апгрейд 2026-08-17 (старый юнит гонял awg-quick внутри netns, которого
    # больше нет) иначе застрял бы со старым ExecStart навсегда.
    unit_content = vpn_probe_unit_content(VPN_PROBE_IFACE, ip_path, awg_quick_path)
    current_unit = (
        _read_privileged(VPN_PROBE_UNIT_FILE) if _privileged_exists(VPN_PROBE_UNIT_FILE) else None
    )
    if current_unit != unit_content:
        if current_unit is not None:
            # Юнит уже active со старым содержимым (живой застрявший апгрейд
            # 2026-08-17: netns-версия) — гасим по СТАРОМУ ExecStop ДО
            # перезаписи файла и daemon-reload. Иначе systemd продолжит
            # считать его active, а `enable --now` ниже не перезапустит уже
            # активный юнит с новым ExecStart сам по себе.
            _sudo(["systemctl", "stop", VPN_PROBE_UNIT_FILE.name])
        with tempfile.NamedTemporaryFile("w", suffix=".service", delete=False) as tmp:
            tmp.write(unit_content)
            tmp_path = Path(tmp.name)
        try:
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
                    str(VPN_PROBE_UNIT_FILE),
                ]
            )
        finally:
            tmp_path.unlink(missing_ok=True)
        _sudo(["systemctl", "daemon-reload"])
    _sudo(["systemctl", "enable", "--now", VPN_PROBE_UNIT_FILE.name])


def make_vpn_probe_tunnel_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="vpn-check-probe-tunnel",
        title="Поднять VPN-туннель пробника (awg-quick, Table=off)",
        needed=_vpn_check_needed,
        check=lambda: _vpn_probe_tunnel_check(settings),
        apply=lambda: _vpn_probe_tunnel_apply(settings),
    )


def _vpn_probe_sudoers_check() -> bool:
    path = SUDOERS_DIR / VPN_PROBE_SUDOERS_FILE
    if not _privileged_exists(path):
        return False
    curl_path = _which("curl")
    if curl_path is None:
        return False
    # Сверяем содержимое, не только факт существования — старый снипет
    # (`ip netns exec vpn-probe curl *`) больше не даёт вызвать реальную
    # команду (netns убрали), апгрейд иначе застрял бы с бесполезным правом.
    expected = vpn_probe_sudoers_content(curl_path, VPN_PROBE_IFACE, getuser())
    return _read_privileged(path) == expected


def _vpn_probe_sudoers_apply(settings: Settings) -> None:
    curl_path = _which("curl")
    if curl_path is None:
        raise FixupError("curl не найден в PATH")
    content = vpn_probe_sudoers_content(curl_path, VPN_PROBE_IFACE, getuser())
    _install_sudoers_snippet(VPN_PROBE_SUDOERS_FILE, content)


def make_vpn_probe_sudoers_fixup(settings: Settings) -> Fixup:
    return Fixup(
        id="vpn-check-probe-sudoers",
        title="Разрешить vpn_check делать curl через интерфейс пробника без пароля",
        needed=_vpn_check_needed,
        check=_vpn_probe_sudoers_check,
        apply=lambda: _vpn_probe_sudoers_apply(settings),
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
        needed=_vpn_needed,
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


def _existing_counter_names() -> set[str]:
    output = _nft_output(["-j", "list", "table", "inet", "filter"])
    if output is None:
        return set()
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return set()
    return {
        counter["name"]
        for item in data.get("nftables", [])
        if (counter := item.get("counter")) is not None
    }


def _proxy_firewall_check() -> bool:
    return {"mtg_bytes", "socks_bytes"} <= _existing_counter_names()


def _append_nftables_conf_counters(settings: Settings) -> None:
    """Дописать счётчики в ПЕРСИСТЕНТНЫЙ /etc/nftables.conf — этот файл
    применяется только при бутстрапе systemd (следующий чистый бут), сам
    файл переписать безопасно; недопустимо только ПРИМЕНЯТЬ его целиком на
    живой ноде (`nft -f`/`systemctl restart nftables`) — этого здесь нет и
    не будет, см. предупреждение выше."""
    result = subprocess.run(
        ["sudo", "cat", str(NFTABLES_CONF_PATH)], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise FixupError(f"не удалось прочитать {NFTABLES_CONF_PATH}: {result.stderr.strip()}")
    content = result.stdout
    if "counter mtg_bytes" in content:
        return
    marker = "chain input {\n"
    idx = content.find(marker)
    if idx == -1:
        raise FixupError(
            f"{NFTABLES_CONF_PATH}: не нашёл 'chain input {{' — структура файла "
            "отличается от ожидаемой, допишите счётчики вручную (см. node/fixups.py "
            "mtg_unit_content/_append_nftables_conf_counters) и повторите nodectl fix"
        )
    insert_at = idx + len(marker)
    addition = (
        "    counter mtg_bytes { }\n"
        "    counter socks_bytes { }\n"
        f"    tcp dport {settings.vpn.mtg_port} counter name mtg_bytes accept\n"
        f'    iifname "tailscale0" tcp dport {settings.vpn.socks_port} '
        "counter name socks_bytes accept\n"
    )
    new_content = content[:insert_at] + addition + content[insert_at:]
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as tmp:
        tmp.write(new_content)
        tmp_path = Path(tmp.name)
    try:
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
    existing = _existing_counter_names()
    if "mtg_bytes" not in existing:
        _sudo(["nft", "add", "counter", "inet", "filter", "mtg_bytes"])
    if "socks_bytes" not in existing:
        _sudo(["nft", "add", "counter", "inet", "filter", "socks_bytes"])
    if _proxy_firewall_check():
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
        needed=_vpn_needed,
        check=_proxy_firewall_check,
        apply=lambda: _proxy_firewall_apply(settings),
    )


def build_fixups(settings: Settings) -> list[Fixup]:
    """Известные фиксы, актуальные для текущих назначений ноды (``needed``)."""
    fixups = [
        INSTALL_SMARTMONTOOLS,
        SMARTCTL_SUDOERS,
        NODE_UNIT_SMARTCTL_PATH,
        JOURNALCTL_GROUP,
        POWER_CONTROL_POLKIT,
        WOL_ENABLE,
        make_awg_sudoers_fixup(settings),
        make_vpn_probe_tunnel_fixup(settings),
        make_vpn_probe_sudoers_fixup(settings),
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
