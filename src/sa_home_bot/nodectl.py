"""nodectl — локальная консоль управления нодой (базовый интерфейс ноды).

Подключается к endpoint'у сервиса ноды по протоколу v0:

    nodectl status                # нода и её службы
    nodectl start|stop|restart X  # управление службой
    nodectl events                # живой хвост событий (Ctrl+C — выход)

Endpoint берётся из --socket (путь unix-сокета или tcp://host:port), либо из
[node].socket указанного --config, либо из ./config.toml, либо дефолт
./data/node.sock. Токен для tcp — [swarm].token конфига.
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
from sa_home_bot.proto.messages import Envelope, ProtoError

DEFAULT_CONFIG = "./config.toml"

_STATUS_ICON = {"running": "✅", "restarting": "🔄", "stopped": "⏹"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodectl", description="Консоль управления нодой sa-home."
    )
    parser.add_argument("--config", "-c", default=None, help="путь к config.toml")
    parser.add_argument(
        "--socket", "-s", default=None, help="endpoint ноды: путь сокета или tcp://host:port"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="статус ноды и её служб")
    for action in ("start", "stop", "restart"):
        p = sub.add_parser(action, help=f"{action} службы")
        p.add_argument("name", help="имя службы (см. nodectl status)")
    sub.add_parser("events", help="живой хвост событий ноды (Ctrl+C — выход)")
    return parser


def _resolve_endpoint(args: argparse.Namespace) -> tuple[Endpoint, str]:
    """Endpoint ноды + токен ([swarm].token — нужен только для tcp)."""
    config_path = args.config
    if config_path is None and Path(DEFAULT_CONFIG).exists():
        config_path = DEFAULT_CONFIG
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
        icon = _STATUS_ICON.get(svc.get("status", ""), "❔")
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
    out = header + "\n" + render_services(state.get("services", []))
    peers = state.get("peers") or []
    if peers:
        lines = [
            f"  {'✅' if p.get('alive') else '⛔'} {p.get('id', '?')} ({p.get('endpoint', '?')})"
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
            print(render_status(await client.get_state()))
        elif args.command in ("start", "stop", "restart"):
            result = await client.command(args.command, {"name": args.name})
            print(render_services([result["service"]]))
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
