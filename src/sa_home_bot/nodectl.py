from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sa_home_bot.config import Settings
from sa_home_bot.devlab import connect as devlab_connect
from sa_home_bot.node.fixups import build_fixups, run_fixups
from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import Endpoint, parse_endpoint, resolve_endpoint
from sa_home_bot.proto.messages import (
    Address,
    Envelope,
    ProtoError,
    ServiceDescription,
)
from sa_home_bot.runtime import format_duration
from sa_home_bot.utils.files import write_private
from sa_home_bot.utils.peer_path import format_peer_path

DEFAULT_CONFIGS = ("./config.toml", "~/.config/sa-home-bot/config.toml")


def _default_config() -> str | None:
    for candidate in DEFAULT_CONFIGS:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None

NODE_WORKDIR = "~/.local/share/sa-home-bot"


def _enter_node_workdir(args: argparse.Namespace) -> None:
    if args.config is not None:
        args.config = str(Path(args.config).expanduser().resolve())
    workdir = Path(NODE_WORKDIR).expanduser()
    if workdir.is_dir():
        os.chdir(workdir)


def _config_path(args: argparse.Namespace) -> str | None:
    return args.config if args.config is not None else _default_config()


def _load_settings(args: argparse.Namespace) -> Settings:
    return Settings.load(_config_path(args))


_STATUS_ICON = {"running": "🟢", "restarting": "🟠", "stopped": "🔴"}

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
    p = sub.add_parser("assign", help="назначить службу ноде (без правки конфига)")
    p.add_argument("name", help="monitor | telegram-bot | apps")
    p = sub.add_parser("unassign", help="снять назначение службы")
    p.add_argument("name", help="имя службы (см. nodectl status)")
    p = sub.add_parser("join", help="присоединиться к рою через уже существующую ноду")
    p.add_argument("endpoint", help="endpoint соседа: tcp://host:port")
    sub.add_parser("restart_node", help="перезапустить саму ноду (супервизор)")
    sub.add_parser("poweroff", help="выключить машину ноды")
    sub.add_parser("reboot", help="перезагрузить машину ноды")
    sub.add_parser("suspend", help="усыпить машину ноды")
    sub.add_parser("events", help="живой хвост событий ноды (Ctrl+C — выход)")
    sub.add_parser(
        "fix",
        help="доустановить программы/права на месте (интерактивный sudo, локально)",
    )
    sub.add_parser(
        "restore-stage",
        help="принять бандл восстановления со stdin (0600) — шлёт `sa-home-bot backup restore`",
    )
    p = sub.add_parser(
        "restore-apply",
        help="применить бандл восстановления vpn-сервера (интерактивный sudo, локально)",
    )
    p.add_argument("--bundle", default=None,
                   help="файл бандла или каталог --dry-run (по умолчанию — принятый restore-stage)")
    p.add_argument("--yes", "-y", action="store_true", help="без вопроса о подтверждении")
    p.add_argument("--wipe-db", action="store_true",
                   help="vpn.sqlite уже не пуст — очистить таблицы снапшота и залить заново")
    sub.add_parser(
        "backup-release",
        help="разрешить этой ноде публиковать бэкап напарнику (новый сервер / первый деплой)",
    )
    p = sub.add_parser("devlab", help="мастерская devlab: подключение этой ноды к хабу")
    p.add_argument(
        "devlab_command", choices=("connect",), help="connect: получить токен и подключить MCP"
    )
    p = sub.add_parser(
        "call", help="вызвать любое умение любой службы роя (то же, что делает Альфред)"
    )
    p.add_argument("service", help="имя службы: torrents, monitor, apps, llm, net, tasks, node")
    p.add_argument("action", help="id действия (см. nodectl describe <служба>)")
    p.add_argument(
        "args",
        nargs="*",
        metavar="ключ=значение",
        help="аргументы действия; true/false/числа распознаются по виду",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="СЕК",
        help="сколько ждать ответа (по умолчанию — обычный таймаут протокола)",
    )
    p = sub.add_parser("describe", help="какие умения объявляет служба и с какими параметрами")
    p.add_argument("service", help="имя службы")
    p = sub.add_parser(
        "llm-probe",
        help="проверить модель службы llm: профиль + пробный chat с рассуждением вкл/выкл",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        metavar="СЕК",
        help="таймаут одного пробного вызова (дефолт 180 — модель может грузиться с диска)",
    )
    sub.add_parser("check_update", help="сверить работающую/установленную/доступную версии")
    sub.add_parser(
        "update", help="pipx install --force до последнего тега (без рестарта процесса)"
    )
    return parser


def _resolve_endpoint(args: argparse.Namespace) -> tuple[Endpoint, str]:
    config_path = _config_path(args)
    settings = Settings.load(config_path)
    if args.socket:
        return parse_endpoint(args.socket), settings.swarm.token
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
        name = svc.get("name", "?")
        if svc.get("external"):
            name += " (внешняя)"
        rows.append(
            (
                name,
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


def _render_peer(peer: dict) -> str:
    icon = "🟢" if peer.get("alive") else "🔴"
    line = f"  {icon} {peer.get('id', '?')} ({peer.get('endpoint', '?')})"
    path = format_peer_path(peer)
    return f"{line} · {path}" if path else line


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
        lines = [_render_peer(p) for p in peers]
        out += "\nПиры:\n" + "\n".join(lines)
    update = state.get("update")
    if update and update.get("restart_required"):
        out += (
            f"\n⚠️ Обновлено до v{update.get('installed')} — "
            f"требуется nodectl restart_node"
        )
    return out


def render_check_update(result: dict) -> str:
    lines = [
        f"Репозиторий: {result.get('repo', '?')}",
        f"Работает: v{result.get('running', '?')}",
        f"На диске: v{result.get('installed') or '?'}",
        f"В репозитории: v{result.get('latest', '?')}",
    ]
    if result.get("installed") == result.get("latest"):
        lines.append("Установлена последняя версия.")
    else:
        lines.append("Доступно обновление — nodectl update.")
    return "\n".join(lines)


def render_update_result(result: dict) -> str:
    if result.get("up_to_date"):
        return f"Уже последняя версия v{result.get('version', '?')}."
    target = result.get("target_version", "?")
    return (
        f"Запущено обновление до v{target} в фоне (файлы на диске, процесс не тронут). "
        f"После завершения — nodectl restart_node."
    )


def render_event(env: Envelope) -> str:
    name = env.payload.get("event", "?")
    data = env.payload.get("data", {})
    details = " ".join(f"{k}={v}" for k, v in data.items())
    stamp = datetime.now().strftime("%H:%M:%S")
    return f"{stamp} {name} {details}".rstrip()


def _run_fix(args: argparse.Namespace) -> int:
    if sys.platform == "win32":
        print(
            "nodectl fix пока не поддерживается на Windows: все рецепты — про "
            "sudoers/пакетные менеджеры Linux. Установку на Windows делает "
            "deploy/install-node.ps1.",
            file=sys.stderr,
        )
        return 1
    settings = _load_settings(args)
    fixups = build_fixups(settings)
    if not fixups:
        print("Все известные фиксы либо не нужны для текущих назначений, либо уже применены.")
        return 0

    print(f"Нужно проверить {len(fixups)} фикс(ов) под назначения {settings.node.assignments}:")
    failed = run_fixups(fixups)

    if failed:
        print(f"\nНе применилось: {', '.join(failed)} — см. вывод выше.", file=sys.stderr)
        return 1
    print("\nГотово. Перезапустите затронутые службы: nodectl restart <name>.")
    return 0


def _staged_bundle_path(settings: Settings) -> Path:
    return Path(settings.node.state_path).parent / "restore-bundle.json"


def _run_restore_stage(args: argparse.Namespace) -> int:
    _enter_node_workdir(args)
    settings = _load_settings(args)
    data = sys.stdin.buffer.read()
    if not data.strip():
        print("Пустой ввод: бандл не получен.", file=sys.stderr)
        return 1
    path = _staged_bundle_path(settings)
    write_private(path, data, make_parents=True)
    print(f"Бандл принят: {path}")
    return 0


def _run_restore_apply(args: argparse.Namespace) -> int:
    from sa_home_bot.backup import apply as backup_apply
    from sa_home_bot.backup import restore as backup_restore

    _enter_node_workdir(args)
    config_path = _config_path(args)
    if config_path is None:
        print("Нет config.toml ноды (укажите --config).", file=sys.stderr)
        return 2
    settings = Settings.load(config_path)
    bundle_path = Path(args.bundle) if args.bundle else _staged_bundle_path(settings)
    try:
        bundle = backup_restore.load_bundle(bundle_path)
        backup_apply.check_bundle(bundle, settings)
        print(backup_restore.summarize_bundle(bundle))
        if not args.yes:
            print("\nНода (служба vpn и sa-home-node) должна быть остановлена.")
            if input("Применить на этой ноде? [y/N] ").strip().lower() not in ("y", "yes", "д"):
                print("Отменено.")
                return 1
        asyncio.run(
            backup_apply.apply_bundle(
                bundle, settings, config_path=Path(config_path), io=backup_apply.SudoIO(),
                wipe_db=args.wipe_db,
            )
        )
    except (backup_restore.RestoreError, backup_apply.ApplyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Не удалось применить: {exc}", file=sys.stderr)
        return 1
    if bundle_path.is_file() and bundle_path == _staged_bundle_path(settings):
        bundle_path.unlink(missing_ok=True)
    print(
        "\nГотово. Дальше: если setup-скрипты ещё не запускали — deploy/setup-awg-jeeves.sh и/или "
        "deploy/setup-reality-server.sh (ключи из восстановленных файлов сохранятся), затем "
        "nodectl fix и запуск ноды: пиры вернёт reconcile()."
    )
    return 0


def _run_backup_release(args: argparse.Namespace) -> int:
    from sa_home_bot.backup.hold import allow_publish

    _enter_node_workdir(args)
    settings = _load_settings(args)
    path = allow_publish(settings, "backup-release")
    print(f"Публикация бэкапа разрешена: {path}")
    return 0


def _service_dst(args: argparse.Namespace) -> Address:
    return Address(node=args.node, service=args.service)


def _call_args(pairs: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep:
            raise ValueError(f"аргумент без «=»: {pair!r}")
        args[key] = _guess_type(raw)
    return args


def _guess_type(raw: str) -> Any:
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    return raw


def render_describe(desc: ServiceDescription) -> str:
    lines = [f"Служба {desc.info.service} на {desc.info.node} (v{desc.info.version})"]
    for action in desc.actions:
        params = ", ".join(
            f"{p.name}{'' if p.required else '?'}:{p.type}"
            + (f" [{'|'.join(p.choices)}]" if p.choices else "")
            for p in action.params
        )
        lines.append(f"  {action.id:16} {action.title}" + (f"  ({params})" if params else ""))
    return "\n".join(lines)


async def _llm_probe(client: ProtoClient, dst: Address, timeout: float) -> None:
    desc = await client.describe(dst=dst)
    model = desc.capabilities[0] if desc.capabilities else "?"
    prof = desc.model_profile or {}
    print(f"Модель:  {model}")
    if prof:
        print(
            f"Профиль: {prof.get('name')} (router={prof.get('router')}, "
            f"num_ctx={prof.get('num_ctx')}, thinking_hidden={prof.get('thinking_hidden')})"
        )
    else:
        print("Профиль: служба не сообщает (старая версия?)")
    messages = [{"role": "user", "content": "Привет"}]
    for reason in ("off", "high"):
        started = time.monotonic()
        try:
            res = await client.command(
                "chat", {"messages": messages, "reason": reason}, dst=dst, timeout=timeout
            )
            dt = time.monotonic() - started
            text = (res.get("response") or "").replace("\n", " ")
            print(f"  reason={reason:4} → {dt:5.1f}s  ok: {text[:70]}")
        except ProtoError as exc:
            dt = time.monotonic() - started
            print(f"  reason={reason:4} → {dt:5.1f}s  ОШИБКА {exc.code}: {exc}")


async def _run(args: argparse.Namespace) -> int:
    endpoint, token = _resolve_endpoint(args)
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
        elif args.command in ("start", "stop", "restart", "assign"):
            result = await client.command(args.command, {"name": args.name}, dst=dst)
            print(render_services([result["service"]]))
        elif args.command == "unassign":
            result = await client.command(args.command, {"name": args.name}, dst=dst)
            print(f"Снято: {result.get('unassigned', args.name)}")
        elif args.command == "join":
            result = await client.command("join", {"endpoint": args.endpoint}, dst=dst)
            added = result.get("peers_added") or []
            print(
                f"Присоединились через {args.endpoint}. "
                f"Новых пиров: {', '.join(added) if added else 'нет'}."
            )
        elif args.command == "call":
            extra = {"timeout": args.timeout} if args.timeout else {}
            result = await client.command(
                args.action, _call_args(args.args), dst=_service_dst(args), **extra
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "describe":
            desc = await client.describe(dst=_service_dst(args))
            print(render_describe(desc))
        elif args.command == "llm-probe":
            await _llm_probe(client, Address(node=args.node, service="llm"), args.timeout)
        elif args.command == "check_update":
            print(render_check_update(await client.command("check_update", dst=dst)))
        elif args.command == "update":
            print(render_update_result(await client.command("update", dst=dst)))
        elif args.command in NO_ARG_ACTIONS:
            result = await client.command(args.command, dst=dst)
            where = f"нода {args.node}" if args.node else "нода"
            print(f"Принято: {where} выполнит {result.get('scheduled', args.command)} "
                  f"через {result.get('delay_s', '?')} с.")
        elif events_mode:
            info = await client.hello()
            print(f"События ноды {info.node} (Ctrl+C — выход):", flush=True)
            await client.join()
            print("Соединение закрыто нодой.", file=sys.stderr)
    except ProtoError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        return 1
    except TimeoutError:
        print("Нода не ответила вовремя — попробуйте ещё раз.", file=sys.stderr)
        return 1
    finally:
        await client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "fix":
        return _run_fix(args)
    if args.command == "restore-stage":
        return _run_restore_stage(args)
    if args.command == "restore-apply":
        return _run_restore_apply(args)
    if args.command == "backup-release":
        return _run_backup_release(args)
    if args.command == "devlab":
        return asyncio.run(devlab_connect.connect(_load_settings(args)))
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
    except FileNotFoundError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
