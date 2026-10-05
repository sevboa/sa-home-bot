"""`sa-home-bot backup keygen` — пара ключей для бэкапа identity vpn-серверов (Этап 39.0.8).

Запускать на alfred: приватный ключ остаётся там (файл 0600, на экран не выводится),
публичный печатается — его кладут в ``[backup].recipient_public_key`` vpn-нод.
Конфиг для keygen не нужен.

``sa-home-bot [-c конфиг] backup list <нода>`` и ``backup restore <нода> --dry-run ДИР |
--apply`` — восстановление vpn-сервера из копии напарника (39.0.8(d), backup/restore.py).
Нужен конфиг alfred: ``[backup].private_key_file`` и доступ к своей ноде роя.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from sa_home_bot.backup.sealed import (
    SealedError,
    dump_key,
    generate_keypair,
    read_private_key,
    write_private_key,
)

HOLDER_HELP = "нода-хранитель копии (по умолчанию — ищем среди живых пиров)"
DEFAULT_KEY_PATH = "~/.config/sa-home-bot/backup.key"


def add_backup_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("backup", help="бэкап identity vpn-серверов")
    sub = parser.add_subparsers(dest="backup_command", required=True)
    kg = sub.add_parser("keygen", help="сгенерировать пару ключей получателя бэкапов")
    kg.add_argument("--out", default=DEFAULT_KEY_PATH,
                    help=f"куда записать приватный ключ (0600), по умолчанию {DEFAULT_KEY_PATH}")
    ls = sub.add_parser("list", help="какие копии ноды лежат у напарника")
    ls.add_argument("node", help="id ноды, чью копию смотрим (jeeves | wooster)")
    ls.add_argument("--holder", default=None, help=HOLDER_HELP)
    rs = sub.add_parser("restore", help="восстановить vpn-сервер из копии напарника")
    rs.add_argument("node", help="id восстанавливаемой ноды")
    mode = rs.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", metavar="КАТАЛОГ",
                      help="только расшифровать и разложить в каталог (0700/0600), ноды не трогать")
    mode.add_argument("--apply", action="store_true",
                      help="применить на целевой ноде по ssh (интерактивный sudo там)")
    rs.add_argument("--snapshot", default=None, metavar="last_nonempty|latest|МЕТКА",
                    help="какой снапшот БД (по умолчанию latest; пустой latest — отказ)")
    rs.add_argument("--identity", default="latest", metavar="latest|МЕТКА",
                    help="какая identity (метки — из `backup list`)")
    rs.add_argument("--no-snapshot", action="store_true", help="только identity, без БД")
    rs.add_argument("--holder", default=None, help=HOLDER_HELP)
    rs.add_argument("--ssh-host", default=None, help="куда ssh (по умолчанию — id ноды)")
    rs.add_argument("--remote-nodectl", default=None, help="путь к nodectl на целевой ноде")
    rs.add_argument("--wipe-db", action="store_true",
                    help="на цели vpn.sqlite уже не пуст — очистить и залить заново")
    rs.add_argument("--yes", "-y", action="store_true", help="без вопроса о подтверждении")
    parser.set_defaults(_run=run)


def run(args: argparse.Namespace) -> int:
    if args.backup_command in ("list", "restore"):
        from sa_home_bot.backup.restore import RestoreError
        from sa_home_bot.proto.messages import ProtoError

        try:
            return asyncio.run(_run_restore(args))
        except (RestoreError, SealedError, ProtoError) as exc:
            print(f"Ошибка: {exc}", file=sys.stderr)
            return 1
        except (ConnectionError, OSError, TimeoutError) as exc:
            print(f"Нет связи с нодой/хранителем: {exc}", file=sys.stderr)
            return 1
    return _keygen(args)


def _load_private(settings: Any) -> bytes:
    path = settings.backup.private_key_file.strip()
    if not path:
        raise SealedError("[backup].private_key_file не задан — расшифровывать нечем")
    return read_private_key(Path(path))


async def _open_ask(settings: Any, config_path: str | None, holder: str | None, node: str):
    """Клиент к своей ноде + функция «спросить службу vpn у хранителя».

    Хранитель — ``--holder`` или первый живой пир, у которого есть копия ``node``.
    Возвращает ``(client, ask, holder)``.
    """
    from sa_home_bot.backup.restore import RestoreError
    from sa_home_bot.backup.serve import ACTION_STORE_LIST
    from sa_home_bot.nodectl import _default_config
    from sa_home_bot.proto.client import ProtoClient
    from sa_home_bot.proto.endpoints import resolve_endpoint
    from sa_home_bot.proto.messages import Address, ProtoError

    path = config_path or _default_config()
    base = Path(path).resolve().parent if path else None
    client = ProtoClient(resolve_endpoint(settings.node.socket, base), token=settings.swarm.token)
    await client.connect()

    def asker(name: str):
        async def ask(action: str, args: dict) -> dict:
            return await client.command(
                action, args, dst=Address(node=name, service="vpn"), timeout=30.0
            )
        return ask

    if holder:
        return client, asker(holder), holder
    state = await client.get_state()
    candidates = [p["id"] for p in state.get("peers", []) if p.get("alive") and p["id"] != node]
    for name in candidates:
        try:
            await asker(name)(ACTION_STORE_LIST, {"node": node})
        except (ProtoError, TimeoutError):
            continue
        return client, asker(name), name
    await client.close()
    raise RestoreError(
        f"не нашёл живого хранителя копии {node} среди пиров {candidates or 'нет'} — "
        "укажите --holder"
    )


def _format_versions(listing: dict) -> str:
    lines = []
    for title, key, date_key in (("identity", "identity", "updated_at"),
                                 ("снапшот БД", "snapshot", "taken_at")):
        lines.append(f"{title}:")
        for v in listing.get(key, []):
            m = v["meta"]
            extra = ""
            if key == "snapshot":
                rows = m.get("rows") or {}
                extra = f"  пиров {rows.get('vpn_peers', '?')}" + (
                    "  ПУСТОЙ" if m.get("empty") else ""
                )
            when = m.get(date_key) or m.get("stored_at") or "?"
            lines.append(f"  {v['label']:34} {when}{extra}")
        if not listing.get(key):
            lines.append("  —")
    return "\n".join(lines)


async def _run_restore(args: argparse.Namespace) -> int:
    from sa_home_bot.backup import restore as rs
    from sa_home_bot.config import Settings

    settings = Settings.load(args.config)
    client, ask, holder = await _open_ask(settings, args.config, args.holder, args.node)
    try:
        if args.backup_command == "list":
            listing = await rs.list_versions(ask, args.node)
            print(f"Копии ноды {args.node} у {holder}:\n" + _format_versions(listing))
            return 0
        key = _load_private(settings)
        bundle = await rs.fetch_bundle(
            ask, args.node, key, identity_label=args.identity,
            snapshot_label=args.snapshot, with_snapshot=not args.no_snapshot,
        )
        print(rs.summarize_bundle(bundle))
        if args.dry_run:
            written = rs.write_dir(Path(args.dry_run), bundle)
            print(f"\nРазложено в {Path(args.dry_run).expanduser()}: "
                  + ", ".join(p.name for p in written) + " (каталог 0700, файлы 0600)."
                  "\nНоды не тронуты. Удалите каталог после проверки: там приватные ключи.")
            return 0
        return _apply_remote(args, bundle)
    finally:
        await client.close()


def _apply_remote(args: argparse.Namespace, bundle: dict) -> int:
    from sa_home_bot.backup import restore as rs

    host = args.ssh_host or args.node
    remote = args.remote_nodectl or rs.REMOTE_NODECTL
    print(f"\nЦель: ssh {host}. Бандл (с приватными ключами) уйдёт по ssh, на цели — 0600.")
    if not args.yes and input("Применить? [y/N] ").strip().lower() not in ("y", "yes", "д"):
        print("Отменено.")
        return 1
    rs.stage_over_ssh(host, rs.bundle_to_bytes(bundle), remote)
    extra = "--wipe-db" if args.wipe_db else ""
    code = rs.apply_over_ssh(host, remote, extra)
    if code != 0:
        print(f"restore-apply на {host} завершился кодом {code}; бандл остался на цели "
              "(повторить: ssh -t … nodectl restore-apply), удалите его после.", file=sys.stderr)
    return code


def _keygen(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    if out.exists():
        print(f"Файл {out} уже существует — не затираю (удалите вручную, если нужно).",
              file=sys.stderr)
        return 1
    private, public = generate_keypair()
    try:
        write_private_key(out, private)
    except (OSError, SealedError) as exc:
        print(f"Не удалось записать ключ: {exc}", file=sys.stderr)
        return 1
    print(f"Приватный ключ записан в {out} (права 0600). Храните только на alfred:")
    print(f'  [backup]\n  private_key_file = "{out}"')
    print("Публичный ключ — для vpn-нод (jeeves/wooster):")
    print(f'  [backup]\n  recipient_public_key = "{dump_key(public)}"')
    return 0
