from __future__ import annotations

import subprocess
from getpass import getuser
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.devlab import protocol
from sa_home_bot.node.fixups import needs
from sa_home_bot.node.fixups.core import Fixup, FixupError
from sa_home_bot.node.fixups.privileged import (
    SUDOERS_DIR,
    file_matches,
    find_executable,
    install_package,
    install_root_file,
    install_sudoers_snippet,
    path_exists,
    read_text_if_exists,
    run_sudo,
)

PODMAN_PACKAGES = ("podman", "uidmap", "passt")
DEVLAB_WRAPPER_PATH = Path("/usr/local/libexec/sa-home-devlab")
DEVLAB_SUDOERS_FILE = "50-sa-home-devlab"
LINGER_DIR = Path("/var/lib/systemd/linger")
SUBUID_FILE = Path("/etc/subuid")
SUBGID_FILE = Path("/etc/subgid")
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
CPU_WEIGHT_DROPIN = "50-sa-home-devlab.conf"
DELEGATE_DROPIN = "50-sa-home-devlab-delegate.conf"
DELEGATED_CONTROLLERS = "cpu cpuset io memory pids"
CGROUP_ROOT = Path("/sys/fs/cgroup")
SUBID_FIRST = 100000
SUBID_COUNT = 65536


def devlab_home(settings: Settings) -> str:
    return f"{settings.devlab.root}/home"


def _podman_check() -> bool:
    return find_executable("podman") is not None and find_executable("newuidmap") is not None


def _podman_apply() -> None:
    for package in PODMAN_PACKAGES:
        install_package(package, f"неизвестен пакетный менеджер для установки {package}")


INSTALL_PODMAN = Fixup(
    id="install-podman",
    title="Установить podman (контейнеры мастерской devlab)",
    needed=needs.devlab,
    check=_podman_check,
    apply=_podman_apply,
)


def _user_exists(user: str) -> bool:
    return subprocess.run(["getent", "passwd", user], capture_output=True).returncode == 0


def make_devlab_user_fixup(settings: Settings) -> Fixup:
    user = settings.devlab.user

    def apply() -> None:
        run_sudo(["install", "-d", "-m", "0755", settings.devlab.root])
        run_sudo(
            [
                "useradd",
                "--system",
                "--user-group",
                "--create-home",
                "--home-dir",
                devlab_home(settings),
                "--shell",
                "/usr/sbin/nologin",
                user,
            ]
        )

    return Fixup(
        id="devlab-user",
        title=f"Создать пользователя {user} (дом в {devlab_home(settings)})",
        needed=needs.devlab,
        check=lambda: _user_exists(user),
        apply=apply,
    )


def subid_owners(content: str) -> set[str]:
    return {line.split(":", 1)[0] for line in content.splitlines() if line.count(":") == 2}


def next_free_subid(*contents: str) -> int:
    end = SUBID_FIRST
    for content in contents:
        for line in content.splitlines():
            parts = line.split(":")
            if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                end = max(end, int(parts[1]) + int(parts[2]))
    return end


def _read_subids() -> tuple[str, str]:
    try:
        return SUBUID_FILE.read_text(), SUBGID_FILE.read_text()
    except FileNotFoundError:
        return "", ""


def make_devlab_subids_fixup(settings: Settings) -> Fixup:
    user = settings.devlab.user

    def check() -> bool:
        uids, gids = _read_subids()
        return user in subid_owners(uids) and user in subid_owners(gids)

    def apply() -> None:
        if not _user_exists(user):
            raise FixupError(f"пользователя {user} нет — сначала devlab-user")
        uids, gids = _read_subids()
        first = next_free_subid(uids, gids)
        span = f"{first}-{first + SUBID_COUNT - 1}"
        run_sudo(["usermod", "--add-subuids", span, "--add-subgids", span, user])

    return Fixup(
        id="devlab-subids",
        title=f"Выдать {user} диапазоны subuid/subgid (rootless podman)",
        needed=needs.devlab,
        check=check,
        apply=apply,
    )


def make_devlab_linger_fixup(settings: Settings) -> Fixup:
    user = settings.devlab.user
    return Fixup(
        id="devlab-linger",
        title=f"Включить linger для {user} (контейнеры живут без входа)",
        needed=needs.devlab,
        check=lambda: path_exists(LINGER_DIR / user),
        apply=lambda: run_sudo(["loginctl", "enable-linger", user]),
    )


def _user_uid(user: str) -> str | None:
    result = subprocess.run(["id", "-u", user], capture_output=True, text=True)
    uid = result.stdout.strip()
    return uid if result.returncode == 0 and uid.isdigit() else None


def cpu_weight_dropin_content() -> str:
    return f"[Slice]\nCPUWeight={protocol.DEVLAB_SLICE_CPU_WEIGHT}\n"


def cpu_weight_dropin_path(uid: str) -> Path:
    return SYSTEMD_UNIT_DIR / f"user-{uid}.slice.d" / CPU_WEIGHT_DROPIN


def make_devlab_cpu_weight_fixup(settings: Settings) -> Fixup:
    user = settings.devlab.user

    def check() -> bool:
        uid = _user_uid(user)
        return uid is not None and file_matches(
            cpu_weight_dropin_path(uid), cpu_weight_dropin_content()
        )

    def apply() -> None:
        uid = _user_uid(user)
        if uid is None:
            raise FixupError(f"пользователя {user} нет — сначала devlab-user")
        install_root_file(
            cpu_weight_dropin_path(uid), cpu_weight_dropin_content(), mode="0644", make_parents=True
        )
        run_sudo(["systemctl", "daemon-reload"])

    return Fixup(
        id="devlab-cpu-weight",
        title=(
            f"Вес CPU {protocol.DEVLAB_SLICE_CPU_WEIGHT} для user-слайса {user} "
            "(контейнеры уступают живым задачам бота)"
        ),
        needed=needs.devlab,
        check=check,
        apply=apply,
    )


def delegate_dropin_content() -> str:
    return f"[Service]\nDelegate={DELEGATED_CONTROLLERS}\n"


def delegate_dropin_path(uid: str) -> Path:
    return SYSTEMD_UNIT_DIR / f"user@{uid}.service.d" / DELEGATE_DROPIN


def user_manager_controllers_path(uid: str) -> Path:
    return (
        CGROUP_ROOT
        / "user.slice"
        / f"user-{uid}.slice"
        / f"user@{uid}.service"
        / "cgroup.controllers"
    )


def _manager_has_cpuset(uid: str) -> bool:
    content = read_text_if_exists(user_manager_controllers_path(uid))
    return content is not None and "cpuset" in content.split()


def make_devlab_delegate_fixup(settings: Settings) -> Fixup:
    user = settings.devlab.user

    def check() -> bool:
        uid = _user_uid(user)
        return (
            uid is not None
            and file_matches(delegate_dropin_path(uid), delegate_dropin_content())
            and _manager_has_cpuset(uid)
        )

    def apply() -> None:
        uid = _user_uid(user)
        if uid is None:
            raise FixupError(f"пользователя {user} нет — сначала devlab-user")
        unit = f"user@{uid}.service"
        install_root_file(
            delegate_dropin_path(uid), delegate_dropin_content(), mode="0644", make_parents=True
        )
        run_sudo(["systemctl", "daemon-reload"])
        run_sudo(["systemctl", "restart", unit])
        if subprocess.run(["sudo", "-n", "systemctl", "is-active", "--quiet", unit]).returncode:
            run_sudo(["systemctl", "start", unit])

    return Fixup(
        id="devlab-delegate",
        title=(
            f"Делегировать cgroup-контроллеры ({DELEGATED_CONTROLLERS}) менеджеру user@ {user} "
            "(rootless podman: cpuset; перезапускает user@, останавливает его контейнеры)"
        ),
        needed=needs.devlab,
        check=check,
        apply=apply,
    )


def devlab_wrapper_content(settings: Settings) -> str:
    home = devlab_home(settings)
    return f"""#!/bin/sh
set -eu
umask 077
export HOME={home}
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
cd "$HOME"
WORK="$HOME/work"
fail() {{ echo "sa-home-devlab: $*" >&2; exit 2; }}
name() {{ case "${{1:-}}" in ""|-*|*[!a-z0-9-]*) fail "плохое имя: ${{1:-}}";; esac; }}
number() {{ case "${{1:-}}" in ""|*[!0-9.]*) fail "не число: ${{1:-}}";; esac; }}
cpuset() {{ case "${{1:-}}" in ""|[!0-9]*|*[!0-9,-]*) fail "не список ядер: ${{1:-}}";; esac; }}
filename() {{
    case "${{1:-}}" in ""|.|..|*[!A-Za-z0-9._-]*) fail "плохое имя файла: ${{1:-}}";; esac
}}
size() {{ case "${{1:-}}" in ""|*[!0-9kmg]*) fail "не размер: ${{1:-}}";; esac; }}
cmd="${{1:-}}"
if [ $# -gt 0 ]; then shift; fi
case "$cmd" in
sync-in)
    name "${{1:-}}"
    rm -rf "${{WORK:?}}/$1"
    mkdir -p "$WORK/$1"
    exec tar -x --no-same-owner -C "$WORK/$1"
    ;;
refresh)
    name "${{1:-}}"
    mkdir -p "$WORK/$1"
    find "$WORK/$1" -mindepth 1 -delete
    exec tar -x --no-same-owner -C "$WORK/$1"
    ;;
sync-out)
    name "${{1:-}}"
    if [ $# -ge 2 ]; then
        filename "$2"
        exec tar -c -C "$WORK/$1" -- "$2"
    fi
    exec tar -c -C "$WORK/$1" .
    ;;
clean)
    name "${{1:-}}"
    rm -f "${{WORK:?}}/$1.containerfile"
    exec rm -rf "${{WORK:?}}/$1"
    ;;
build)
    name "${{1:-}}"; name "${{2:-}}"
    spec="$WORK/$1.containerfile"
    cat > "$spec"
    status=0
    podman build --tag "devlab/$2" --file "$spec" "$WORK/$1" || status=$?
    rm -f "$spec"
    exit "$status"
    ;;
create)
    name "${{1:-}}"; name "${{2:-}}"; number "${{3:-}}"; size "${{4:-}}"
    set_cpus=""
    if [ $# -ge 5 ]; then
        cpuset "$5"
        set_cpus="$5"
    fi
    exec podman run --detach --name "devlab-$1" --network none --cpus "$3" --memory "$4" \\
        ${{set_cpus:+--cpuset-cpus "$set_cpus"}} \\
        --pids-limit 4096 --volume "$WORK/$1:/work" --workdir /work "devlab/$2" sleep infinity
    ;;
start)
    name "${{1:-}}"
    exec podman start "devlab-$1"
    ;;
stop)
    name "${{1:-}}"
    exec podman stop --time 5 "devlab-$1"
    ;;
rm)
    name "${{1:-}}"
    exec podman rm --force "devlab-$1"
    ;;
pause|unpause)
    if [ $# -ge 1 ]; then
        name "$1"
        exec podman "$cmd" "devlab-$1"
    fi
    if [ "$cmd" = pause ]; then state=running; else state=paused; fi
    ids="$(podman ps --quiet --filter name=devlab- --filter "status=$state")"
    [ -n "$ids" ] || exit 0
    exec podman "$cmd" $ids
    ;;
interrupt)
    name "${{1:-}}"
    exec podman exec "devlab-$1" sh -c 'kill -TERM -1; sleep 2; kill -KILL -1'
    ;;
exec)
    name "${{1:-}}"; number "${{2:-}}"
    id="$1"; limit="$2"; shift 2
    [ $# -gt 0 ] || fail "нет команды"
    exec podman exec "devlab-$id" timeout "$limit" sh -c "$*"
    ;;
ps)
    exec podman ps --all --filter name=devlab- --format json
    ;;
images)
    exec podman images --filter 'reference=devlab/*' --format json
    ;;
prune)
    exec podman image prune --force
    ;;
*)
    fail "подкоманды: sync-in refresh sync-out clean build create start stop rm pause" \\
        "unpause interrupt exec ps images prune"
    ;;
esac
"""


def devlab_sudoers_content(settings: Settings, user: str) -> str:
    return f"{user} ALL=({settings.devlab.user}) NOPASSWD: {DEVLAB_WRAPPER_PATH} *\n"


def make_devlab_sudoers_fixup(settings: Settings) -> Fixup:
    user = getuser()

    def check() -> bool:
        return file_matches(DEVLAB_WRAPPER_PATH, devlab_wrapper_content(settings)) and file_matches(
            SUDOERS_DIR / DEVLAB_SUDOERS_FILE, devlab_sudoers_content(settings, user)
        )

    def apply() -> None:
        install_root_file(
            DEVLAB_WRAPPER_PATH, devlab_wrapper_content(settings), mode="0755", make_parents=True
        )
        install_sudoers_snippet(DEVLAB_SUDOERS_FILE, devlab_sudoers_content(settings, user))

    return Fixup(
        id="devlab-sudoers",
        title=f"Обёртка {DEVLAB_WRAPPER_PATH} и sudo без пароля от {settings.devlab.user}",
        needed=needs.devlab,
        check=check,
        apply=apply,
    )


def devlab_fixups(settings: Settings) -> list[Fixup]:
    return [
        INSTALL_PODMAN,
        make_devlab_user_fixup(settings),
        make_devlab_subids_fixup(settings),
        make_devlab_linger_fixup(settings),
        make_devlab_cpu_weight_fixup(settings),
        make_devlab_delegate_fixup(settings),
        make_devlab_sudoers_fixup(settings),
    ]
