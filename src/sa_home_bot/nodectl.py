"""nodectl — локальная консоль управления нодой (базовый интерфейс ноды).

Подключается к endpoint'у сервиса ноды по протоколу v0:

    nodectl status                # нода и её службы
    nodectl start|stop|restart X  # управление службой
    nodectl restart_node          # перезапустить саму ноду (не службу)
    nodectl poweroff|reboot|suspend  # питание машины
    nodectl events                # живой хвост событий (Ctrl+C — выход)
    nodectl -n winpc status       # то же о ноде winpc («спроси любого»:
                                  # запрос идёт своей ноде, та пересылает)

Endpoint берётся из --socket (путь unix-сокета или tcp://host:port), либо из
[node].socket указанного --config, либо из первого найденного конфига по
умолчанию (./config.toml, потом ~/.config/sa-home-bot/config.toml), либо
дефолт ./data/node.sock. Токен для tcp — [swarm].token конфига.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from sa_home_bot.config import Settings
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import Endpoint, parse_endpoint, resolve_endpoint
from sa_home_bot.proto.messages import Address, Envelope, ProtoError
from sa_home_bot.runtime import format_duration

# Кандидаты конфига без --config: рабочий каталог (разработка на alfred),
# затем XDG-путь (нода, установленная через pipx).
DEFAULT_CONFIGS = ("./config.toml", "~/.config/sa-home-bot/config.toml")


def _default_config() -> str | None:
    for candidate in DEFAULT_CONFIGS:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None

_STATUS_ICON = {"running": "🟢", "restarting": "🟠", "stopped": "🔴"}

# Действия без параметров, объявленные динамически (describe): нода может
# их не поддерживать (напр. restart_node без колбэка) — тогда сервер вернёт
# unknown_action, а не молчаливо съест команду.
NO_ARG_ACTIONS = ("restart_node", "poweroff", "reboot", "suspend")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodectl", description="Консоль управления нодой sa-home."
    )
    parser.add_argument("--config", "-c", default=None, help="путь к config.toml")
    parser.add_argument(
        "--socket", "-s", default=None, help="endpoint ноды: путь сокета или tcp://host:port"
    )
    parser.add_argument(
        "--node",
        "-n",
        default=None,
        help="id удалённой ноды роя: запрос пойдёт через свою ноду",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="статус ноды и её служб")
    for action in ("start", "stop", "restart"):
        p = sub.add_parser(action, help=f"{action} службы")
        p.add_argument("name", help="имя службы (см. nodectl status)")
    sub.add_parser("restart_node", help="перезапустить саму ноду (супервизор)")
    sub.add_parser("poweroff", help="выключить машину ноды")
    sub.add_parser("reboot", help="перезагрузить машину ноды")
    sub.add_parser("suspend", help="усыпить машину ноды")
    sub.add_parser("events", help="живой хвост событий ноды (Ctrl+C — выход)")
    return parser


def _resolve_endpoint(args: argparse.Namespace) -> tuple[Endpoint, str]:
    """Endpoint ноды + токен ([swarm].token — нужен только для tcp)."""
    config_path = args.config if args.config is not None else _default_config()
    settings = Settings.load(config_path)
    if args.socket:
        return parse_endpoint(args.socket), settings.swarm.token
    # Относительный путь в конфиге — относительно каталога конфига, а не CWD:
    # так `nodectl -c ~/proj/config.toml status` работает из любого каталога.
    base = Path(config_path).resolve().parent if config_path is not None else None
    return resolve_endpoint(settings.node.socket, base), settings.swarm.token


def _fmt_started(iso: str | None) -> str:
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def render_services(services: list[dict]) -> str:
    if not services:
        return "Службы не назначены."
    rows = [("СЛУЖБА", "СОСТОЯНИЕ", "PID", "РЕСТАРТЫ", "ЗАПУЩЕНА")]
    for svc in services:
        icon = _STATUS_ICON.get(svc.get("status", ""), "⚪")
        rows.append(
            (
                svc.get("name", "?"),
                f"{icon} {svc.get('status', '?')}",
                str(svc.get("pid") or "—"),
                str(svc.get("restarts", 0)),
                _fmt_started(svc.get("started_at")),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        for row in rows
    )


def render_status(state: dict) -> str:
    header = f"Нода {state.get('node', '?')} (v{state.get('version', '?')})"
    uptime_bits = []
    if state.get("system_uptime_s") is not None:
        uptime_bits.append(f"система {format_duration(state['system_uptime_s'])}")
    if state.get("uptime_s") is not None:
        uptime_bits.append(f"нода {format_duration(state['uptime_s'])}")
    if uptime_bits:
        header += "\nАптайм: " + " · ".join(uptime_bits)
    out = header + "\n" + render_services(state.get("services", []))
    peers = state.get("peers") or []
    if peers:
        lines = [
            f"  {'🟢' if p.get('alive') else '🔴'} {p.get('id', '?')} ({p.get('endpoint', '?')})"
            for p in peers
        ]
        out += "\nПиры:\n" + "\n".join(lines)
    return out


def render_event(env: Envelope) -> str:
    name = env.payload.get("event", "?")
    data = env.payload.get("data", {})
    details = " ".join(f"{k}={v}" for k, v in data.items())
    stamp = datetime.now().strftime("%H:%M:%S")
    return f"{stamp} {name} {details}".rstrip()


async def _run(args: argparse.Namespace) -> int:
    endpoint, token = _resolve_endpoint(args)
    # -n/--node: адресат в конверте, пересылку делает своя нода (§11 п. 2).
    dst = Address(node=args.node, service="node") if args.node else None

    events_mode = args.command == "events"
    printed = asyncio.Event()

    async def on_event(env: Envelope) -> None:
        print(render_event(env), flush=True)
        printed.set()

    client = ProtoClient(endpoint, token=token, on_event=on_event if events_mode else None)
    try:
        await client.connect()
    except (ConnectionError, OSError, ProtoError) as exc:
        print(f"Нода недоступна ({endpoint}): {exc}", file=sys.stderr)
        return 1

    try:
        if args.command == "status":
            print(render_status(await client.get_state(dst=dst)))
        elif args.command in ("start", "stop", "restart"):
            result = await client.command(args.command, {"name": args.name}, dst=dst)
            print(render_services([result["service"]]))
        elif args.command in NO_ARG_ACTIONS:
            result = await client.command(args.command, dst=dst)
            where = f"нода {args.node}" if args.node else "нода"
            print(f"Принято: {where} выполнит {result.get('scheduled', args.command)} "
                  f"через {result.get('delay_s', '?')} с.")
        elif events_mode:
            info = await client.hello()
            print(f"События ноды {info.node} (Ctrl+C — выход):", flush=True)
            await client.join()  # живём, пока сервер не закроет соединение
            print("Соединение закрыто нодой.", file=sys.stderr)
    except ProtoError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        return 1
    finally:
        await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
