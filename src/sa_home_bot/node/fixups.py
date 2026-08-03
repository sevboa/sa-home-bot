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
import os
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
from sa_home_bot.sensors.disks import SMARTCTL_REQUIREMENT
from sa_home_bot.utils.requirements import install_argv

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


def build_fixups(settings: Settings) -> list[Fixup]:
    """Известные фиксы, актуальные для текущих назначений ноды (``needed``)."""
    fixups = [
        INSTALL_SMARTMONTOOLS,
        SMARTCTL_SUDOERS,
        JOURNALCTL_GROUP,
        POWER_CONTROL_POLKIT,
        WOL_ENABLE,
        make_awg_sudoers_fixup(settings),
        *(make_apps_unit_fixup(app) for app in settings.apps.items),
    ]
    return [f for f in fixups if f.needed(settings)]


def run_fixups(fixups: list[Fixup]) -> list[str]:
    """Применить фиксы по одному (идемпотентно), печатая прогресс.

    Общий код между ``nodectl fix`` и ``sa-home-bot init`` (который зовёт
    фиксы сам сразу после первой установки workstation-ноды). Возвращает id
    тех, что не удалось применить/подтвердить.
    """
    failed: list[str] = []
    for fixup in fixups:
        if fixup.check():
            print(f"  ✅ {fixup.title} — уже применено")
            continue
        print(f"  ⏳ {fixup.title} — применяю (может спросить пароль sudo)...")
        try:
            fixup.apply()
        except FixupError as exc:
            print(f"  ❌ {fixup.title}: {exc}")
            failed.append(fixup.id)
            continue
        if fixup.check():
            print(f"  ✅ {fixup.title} — применено")
        else:
            print(f"  ⚠️ {fixup.title}: команда прошла, но проверка всё ещё отрицательна")
            failed.append(fixup.id)
    return failed
