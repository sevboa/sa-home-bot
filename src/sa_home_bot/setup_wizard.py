"""`sa-home-bot init` — установка новой ноды без git-checkout (см. cli.py).

Раньше единственный путь завести ноду — это склонировать репозиторий только
за ``config.example.toml`` и ``deploy/sa-home-node.service``, вручную выставить
в них id/kind/token/join и путём проб поправить абсолютные пути. Эта команда
делает то же самое одним вызовом: пишет минимальный ``config.toml`` (только
``[node]``/``[swarm]`` — остальные секции подтягивают дефолты из ``config.py``,
как и раньше) и, на Linux, user-юнит systemd с уже подставленными путями.

Работает и интерактивно (спрашивает недостающее), и флагами — второе для
сценария «поставить ноду по SSH одной командой», где спросить не у кого
(нет tty).
"""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import socket
import sys
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/sa-home-bot/config.toml").expanduser()
DEFAULT_DATA_DIR = Path("~/.local/share/sa-home-bot").expanduser()

KIND_CHOICES = ("server", "workstation", "vps")


def add_init_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "init",
        help="создать config.toml новой ноды (и, на Linux, systemd-юнит) без git-checkout",
        description=__doc__,
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
    # JSON-синтаксис строк и списков строк совпадает с TOML — не изобретаем
    # свой экранизатор ради десятка полей.
    return json.dumps(value, ensure_ascii=False)


def _render_config(*, node_id: str, kind: str, assignments: list[str],
                    listen: list[str], token: str, join: str) -> str:
    return (
        "# Сгенерировано `sa-home-bot init`. Это не единственный источник —\n"
        "# остальные секции берут дефолты из кода (см. config.example.toml в\n"
        "# репозитории, если нужно переопределить что-то ещё).\n"
        "\n"
        "[node]\n"
        f"id = {_toml_scalar(node_id)}\n"
        f"kind = {_toml_scalar(kind)}\n"
        f"assignments = {_toml_scalar(assignments)}\n"
        f"listen = {_toml_scalar(listen)}\n"
        "\n"
        "[swarm]\n"
        f"token = {_toml_scalar(token)}\n"
        f"join = {_toml_scalar(join)}\n"
    )


def _render_systemd_unit(*, exec_path: str, config_path: Path, data_dir: Path) -> str:
    local_bin = str(Path(exec_path).parent)
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


def run_init(args: argparse.Namespace) -> int:
    interactive = not args.non_interactive and sys.stdin.isatty()
    missing_for_noninteractive: list[str] = []

    config_path = Path(args.config).expanduser()
    data_dir = Path(args.data_dir).expanduser()

    node_id = args.node_id
    if node_id is None:
        default_id = socket.gethostname()
        node_id = _prompt("Имя ноды в рое", default_id) if interactive else default_id

    kind = args.kind
    if kind is None:
        if interactive:
            kind = _prompt("Тип машины (server/workstation/vps)", "workstation")
            if kind not in KIND_CHOICES:
                print(f"Неизвестный тип {kind!r}, беру workstation", file=sys.stderr)
                kind = "workstation"
        else:
            kind = "workstation"

    assignments = _split_csv(args.assignments) if args.assignments is not None else None
    if assignments is None:
        assignments = (
            _split_csv(_prompt(
                "Что нода поднимает (через запятую, пусто — просто вступить в рой)", ""
            ))
            if interactive
            else []
        )

    listen = _split_csv(args.listen) if args.listen is not None else None
    if listen is None:
        listen = (
            _split_csv(_prompt(
                "Адрес(а) для входящих соединений от пиров (пусто — только исходящие)", ""
            ))
            if interactive
            else []
        )

    join = args.join
    if join is None:
        join = _prompt(
            "Адрес существующей ноды роя, tcp://host:port (пусто — это первая нода нового роя)", ""
        ) if interactive else ""

    token = args.token
    if token is None:
        if join:
            if interactive:
                token = _prompt(
                    "Токен роя (тот же, что в config.toml существующей ноды — "
                    "см. `grep -A3 '^\\[swarm\\]' " + str(DEFAULT_CONFIG_PATH) + "` на ней)"
                )
                while not token:
                    token = _prompt("Токен обязателен при непустом --join, повтори")
            else:
                missing_for_noninteractive.append("--token (обязателен вместе с --join)")
                token = ""
        else:
            if interactive:
                generated = secrets.token_urlsafe(32)
                if _prompt_yes_no(
                    f"Это первая нода — сгенерировать новый токен роя ({generated[:8]}…)?", True
                ):
                    token = generated
                else:
                    token = _prompt("Вставь токен")
            else:
                token = secrets.token_urlsafe(32)

    if missing_for_noninteractive:
        print(
            "Не хватает значений для --non-interactive: " + ", ".join(missing_for_noninteractive),
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
        _render_config(node_id=node_id, kind=kind, assignments=assignments,
                        listen=listen, token=token, join=join),
        encoding="utf-8",
    )
    (data_dir / "data").mkdir(parents=True, exist_ok=True)
    print(f"Записан {config_path}")
    print(f"Каталог данных {data_dir}/data готов")

    unit_written = False
    if not args.no_systemd_unit and sys.platform.startswith("linux"):
        unit_path = Path("~/.config/systemd/user/sa-home-node.service").expanduser()
        if unit_path.exists() and not args.force:
            print(f"{unit_path} уже существует — не трогаю (--force, чтобы переписать)")
        else:
            exec_path = shutil.which("sa-home-bot") or str(Path(sys.argv[0]).resolve())
            unit_path.parent.mkdir(parents=True, exist_ok=True)
            unit_content = _render_systemd_unit(
                exec_path=exec_path, config_path=config_path, data_dir=data_dir
            )
            unit_path.write_text(unit_content, encoding="utf-8")
            print(f"Записан {unit_path}")
            unit_written = True

    if not join:
        print(f"\nТокен роя (сохрани — понадобится для следующих нод): {token}")

    if unit_written:
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
