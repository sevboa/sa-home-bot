from __future__ import annotations

import argparse
import getpass
import json
import secrets
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.node.fixups import SMARTCTL_WRAPPER_PATH, build_fixups, run_fixups

DEFAULT_CONFIG_PATH = Path("~/.config/sa-home-bot/config.toml").expanduser()
DEFAULT_DATA_DIR = Path("~/.local/share/sa-home-bot").expanduser()

KIND_CHOICES = ("server", "workstation", "vps")

INIT_DESCRIPTION = (
    '`sa-home-bot init` — установка новой ноды без git-checkout (см. cli.py).\n'
    '\n'
    'Раньше единственный путь завести ноду — это склонировать репозиторий только\n'
    'за ``config.example.toml`` и ``deploy/sa-home-node.service``, вручную выставить\n'
    'в них id/kind/token/join и путём проб поправить абсолютные пути. Эта команда\n'
    'делает то же самое одним вызовом: пишет минимальный ``config.toml`` (только\n'
    '``[node]``/``[swarm]`` — остальные секции подтягивают дефолты из ``config.py``,\n'
    'как и раньше) и, на Linux, user-юнит systemd с уже подставленными путями.\n'
    '\n'
    'Работает и интерактивно (спрашивает недостающее), и флагами — второе для\n'
    'сценария «поставить ноду по SSH одной командой», где спросить не у кого\n'
    '(нет tty).\n'
)


def add_init_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="создать config.toml новой ноды (и, на Linux, systemd-юнит) без git-checkout",
        description=INIT_DESCRIPTION,
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"куда писать config.toml (по умолчанию {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help=f"рабочий каталог ноды — там же данные/сокеты (по умолчанию {DEFAULT_DATA_DIR})",
    )
    parser.add_argument("--node-id", default=None, help="имя ноды в рое (по умолчанию — hostname)")
    parser.add_argument("--kind", choices=KIND_CHOICES, default=None,
                         help="тип машины: server (24/7) | workstation (спит/будится) | vps")
    parser.add_argument("--assignments", default=None,
                         help="что нода поднимает, через запятую, напр. 'monitor,apps' "
                         "(пусто — просто вступить в рой)")
    parser.add_argument("--listen", default=None,
                         help="адрес(а), на которых нода слушает пиров, через запятую "
                         "(напр. tcp://<tailscale-ip>:8710); пусто — только исходящие")
    parser.add_argument("--join", default=None,
                         help="endpoint уже существующей ноды роя, напр. tcp://192.168.0.101:8710 "
                         "(пусто — это первая нода нового роя)")
    parser.add_argument("--token", default=None,
                         help="общий секрет роя (тот же, что у остальных нод); "
                         "пусто при пустом --join — сгенерируется новый")
    parser.add_argument("--non-interactive", action="store_true",
                         help="не спрашивать недостающее — падать с ошибкой (для установки по SSH)")
    parser.add_argument("--force", action="store_true",
                         help="перезаписать config.toml/юнит, если уже существуют")
    parser.add_argument("--no-systemd-unit", action="store_true",
                         help="не писать user-юнит systemd (только config.toml)")
    parser.add_argument("--no-start", action="store_true",
                         help="не запускать юнит — только записать файлы и напечатать команды")
    parser.set_defaults(_run=run_init)


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"{question}{suffix}: ").strip()
    except EOFError:
        return default
    return answer or default


def _prompt_yes_no(question: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{question} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "д", "да")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _toml_scalar(value: str | list[str]) -> str:
    return json.dumps(value, ensure_ascii=False)


_SERVICE_PATH_DEFAULTS: dict[str, dict[str, str]] = {
    "database": {"path": "sentinel.sqlite"},
    "monitor": {"socket": "monitor.sock", "db_path": "monitor.sqlite"},
    "apps": {"socket": "apps.sock"},
    "torrents": {"socket": "torrents.sock"},
    "memory": {"socket": "memory.sock", "db_path": "memory.sqlite"},
    "tasks": {"socket": "tasks.sock", "db_path": "tasks.sqlite"},
    "net": {"socket": "net.sock"},
    "llm": {"socket": "llm.sock"},
    "vpn": {"socket": "vpn.sock", "db_path": "vpn.sqlite", "apk_cache_dir": "vpn-apk"},
}


def _render_service_paths(data_dir: Path) -> str:
    lines = []
    for section, fields in _SERVICE_PATH_DEFAULTS.items():
        lines.append(f"[{section}]")
        for field, filename in fields.items():
            absolute = data_dir / "data" / filename
            lines.append(f"{field} = {_toml_scalar(str(absolute))}")
        lines.append("")
    return "\n".join(lines)


def _render_config(*, node_id: str, kind: str, assignments: list[str],
                    listen: list[str], token: str, join: str, data_dir: Path) -> str:
    return (
        "# Сгенерировано `sa-home-bot init`. Это не единственный источник —\n"
        "# остальное (списки приложений, учётные данные, подписки бота) —\n"
        "# дефолты из кода, правь руками (см. config.example.toml в репозитории).\n"
        "\n"
        "[node]\n"
        f"id = {_toml_scalar(node_id)}\n"
        f"kind = {_toml_scalar(kind)}\n"
        f"socket = {_toml_scalar(str(data_dir / 'data' / 'node.sock'))}\n"
        f"assignments = {_toml_scalar(assignments)}\n"
        f"listen = {_toml_scalar(listen)}\n"
        "\n"
        + _render_service_paths(data_dir)
        + "\n[swarm]\n"
        f"token = {_toml_scalar(token)}\n"
        f"join = {_toml_scalar(join)}\n"
    )


def _render_systemd_unit(*, exec_path: str, config_path: Path, data_dir: Path) -> str:
    local_bin = str(SMARTCTL_WRAPPER_PATH.parent)
    return (
        "[Unit]\n"
        "Description=sa-home-node (нода: супервизор служб роя)\n"
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={data_dir}\n"
        "ExecStartPre=/bin/sh -c 'until getent hosts api.telegram.org >/dev/null 2>&1; "
        "do sleep 3; done'\n"
        f"ExecStart={exec_path} --service node --config {config_path}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "TimeoutStartSec=300\n"
        "KillSignal=SIGTERM\n"
        "TimeoutStopSec=150\n"
        f"Environment=PATH={local_bin}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _run_step(cmd: list[str]) -> bool:
    print(f"$ {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print(f"  {cmd[0]} не найден", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        print(f"  упало с кодом {exc.returncode}", file=sys.stderr)
        return False
    return True


def _enable_and_start(*, interactive: bool) -> None:
    print()
    ok = _run_step(["systemctl", "--user", "daemon-reload"])
    ok = _run_step(["systemctl", "--user", "enable", "--now", "sa-home-node"]) and ok

    if interactive:
        _run_step(["sudo", "loginctl", "enable-linger", getpass.getuser()])
    else:
        print(
            "Не в интерактивном режиме — не зову sudo (спросить пароль было бы не у кого).\n"
            f"Выполни сам, чтобы служба пережила выход из сессии: "
            f"sudo loginctl enable-linger {getpass.getuser()}"
        )

    if ok:
        print("\nnodectl status / journalctl --user -u sa-home-node -f — посмотреть, что внутри")
    else:
        print(
            "\nАвтозапуск не задался целиком — доделай руками:\n"
            "  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now sa-home-node"
        )


def _apply_init_fixups(config_path: Path, *, interactive: bool) -> None:
    if not sys.platform.startswith("linux"):
        return
    settings = Settings.load(config_path)
    fixups = build_fixups(settings)
    if not fixups:
        return
    if not interactive:
        print(
            f"\nЕсть {len(fixups)} донастройк(а/и), нужен sudo — не в интерактивном "
            "режиме, спросить пароль не у кого. Выполни отдельно на ноде: nodectl fix"
        )
        return
    print(f"\nДонастраиваю {len(fixups)} шаг(ов) под тип машины/назначения:")
    failed = run_fixups(fixups)
    if failed:
        print(
            f"Не применилось: {', '.join(failed)} — донастрой отдельно: nodectl fix",
            file=sys.stderr,
        )


def _resolve_kind(args: argparse.Namespace, interactive: bool) -> str:
    if args.kind is not None:
        return args.kind
    if not interactive:
        return "workstation"
    kind = _prompt("Тип машины (server/workstation/vps)", "workstation")
    if kind not in KIND_CHOICES:
        print(f"Неизвестный тип {kind!r}, беру workstation", file=sys.stderr)
        return "workstation"
    return kind


def _resolve_assignments(args: argparse.Namespace, interactive: bool) -> list[str]:
    if args.assignments is not None:
        return _split_csv(args.assignments)
    if not interactive:
        return []
    return _split_csv(
        _prompt("Что нода поднимает (через запятую, пусто — просто вступить в рой)", "")
    )


def _resolve_listen(args: argparse.Namespace, interactive: bool, kind: str) -> list[str]:
    if args.listen is not None:
        return _split_csv(args.listen)
    if kind != "vps":
        return ["tcp://0.0.0.0:8710"]
    if not interactive:
        print(
            "listen пуст (kind=vps) — нода не сможет вступить в рой, "
            "пока не задашь --listen явно",
            file=sys.stderr,
        )
        return []
    listen = _split_csv(
        _prompt(
            "Публичная машина (vps) — адрес(а) для входящих соединений от "
            "пиров, НЕ 0.0.0.0: явный приватный/tailscale-адрес, иначе рой "
            "торчит в интернет",
            "",
        )
    )
    while not listen and not _prompt_yes_no(
        "Пусто — нода не сможет вступить в рой вовсе (swarm_join требует наш "
        "адрес). Точно оставить пустым?",
        False,
    ):
        listen = _split_csv(_prompt("Адрес(а) для входящих соединений от пиров", ""))
    return listen


def _resolve_join(args: argparse.Namespace, interactive: bool) -> tuple[str, bool]:
    if args.join is not None:
        return args.join, False
    if not interactive:
        return "", False
    founding_new_swarm = not _prompt_yes_no(
        "В этой локальной сети уже есть рой (другие ноды sa-home-bot)?", True
    )
    if founding_new_swarm:
        return "", True
    join = _prompt(
        "IP:порт соседа, если знаешь (пусто — понадеемся на LAN-маячок: "
        "он сам найдёт и присоединится по общему токену)",
        "",
    )
    return join, False


def _resolve_token(
    args: argparse.Namespace, interactive: bool, join: str, founding_new_swarm: bool
) -> str | None:
    if args.token is not None:
        return args.token
    must_paste_token = bool(join) or (interactive and not founding_new_swarm)
    if not must_paste_token:
        if not interactive:
            return secrets.token_urlsafe(32)
        generated = secrets.token_urlsafe(32)
        if _prompt_yes_no(
            f"Это первая нода — сгенерировать новый токен роя ({generated[:8]}…)?", True
        ):
            return generated
        return _prompt("Вставь токен")
    if not interactive:
        return None
    token = _prompt(
        "Токен роя (тот же, что в config.toml существующей ноды — "
        "см. `grep -A3 '^\\[swarm\\]' " + str(DEFAULT_CONFIG_PATH) + "` на ней)"
    )
    while not token:
        token = _prompt("Токен обязателен, повтори")
    return token


def _write_systemd_unit(*, config_path: Path, data_dir: Path, force: bool) -> bool:
    unit_path = Path("~/.config/systemd/user/sa-home-node.service").expanduser()
    if unit_path.exists() and not force:
        print(f"{unit_path} уже существует — не трогаю (--force, чтобы переписать)")
        return False
    exec_path = shutil.which("sa-home-bot") or str(Path(sys.argv[0]).resolve())
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_content = _render_systemd_unit(
        exec_path=exec_path, config_path=config_path, data_dir=data_dir
    )
    unit_path.write_text(unit_content, encoding="utf-8")
    print(f"Записан {unit_path}")
    return True


def run_init(args: argparse.Namespace) -> int:
    interactive = not args.non_interactive and sys.stdin.isatty()

    config_path = Path(args.config).expanduser()
    data_dir = Path(args.data_dir).expanduser()

    node_id = args.node_id
    if node_id is None:
        default_id = socket.gethostname()
        node_id = _prompt("Имя ноды в рое", default_id) if interactive else default_id

    kind = _resolve_kind(args, interactive)
    assignments = _resolve_assignments(args, interactive)
    listen = _resolve_listen(args, interactive, kind)
    join, founding_new_swarm = _resolve_join(args, interactive)
    token = _resolve_token(args, interactive, join, founding_new_swarm)

    if token is None:
        print(
            "Не хватает значений для --non-interactive: --token (обязателен вместе с --join)",
            file=sys.stderr,
        )
        return 2

    if config_path.exists() and not args.force:
        print(
            f"{config_path} уже существует — не трогаю (передай --force, чтобы переписать, "
            "или другой --config)",
            file=sys.stderr,
        )
        return 1

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        _render_config(
            node_id=node_id,
            kind=kind,
            assignments=assignments,
            listen=listen,
            token=token,
            join=join,
            data_dir=data_dir,
        ),
        encoding="utf-8",
    )
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    print(f"Записан {config_path}")
    print(f"Каталог данных {data_dir}/data готов")

    _apply_init_fixups(config_path, interactive=interactive)

    unit_written = False
    if not args.no_systemd_unit and sys.platform.startswith("linux"):
        unit_written = _write_systemd_unit(
            config_path=config_path, data_dir=data_dir, force=args.force
        )

    if not join:
        print(f"\nТокен роя (сохрани — понадобится для следующих нод): {token}")

    if unit_written and not args.no_start:
        _enable_and_start(interactive=interactive)
    elif unit_written:
        print(
            "\nДальше:\n"
            "  systemctl --user daemon-reload\n"
            "  systemctl --user enable --now sa-home-node\n"
            "  sudo loginctl enable-linger $USER   # службы переживут выход из сессии\n"
            "  journalctl --user -u sa-home-node -f\n"
            "  nodectl status"
        )
    else:
        print(
            f"\nЗапуск вручную: sa-home-bot --service node --config {config_path}\n"
            "(юнит не писался — см. --no-systemd-unit/платформу; на Windows "
            "используй deploy/install-node.ps1)"
        )
    return 0
