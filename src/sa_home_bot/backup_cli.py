"""`sa-home-bot backup keygen` — пара ключей для бэкапа identity vpn-серверов (Этап 39.0.8).

Запускать на alfred: приватный ключ остаётся там (файл 0600, на экран не выводится),
публичный печатается — его кладут в ``[backup].recipient_public_key`` vpn-нод.
Конфиг команде не нужен.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sa_home_bot.backup.sealed import (
    SealedError,
    dump_key,
    generate_keypair,
    write_private_key,
)

DEFAULT_KEY_PATH = "~/.config/sa-home-bot/backup.key"


def add_backup_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("backup", help="бэкап identity vpn-серверов")
    sub = parser.add_subparsers(dest="backup_command", required=True)
    kg = sub.add_parser("keygen", help="сгенерировать пару ключей получателя бэкапов")
    kg.add_argument("--out", default=DEFAULT_KEY_PATH,
                    help=f"куда записать приватный ключ (0600), по умолчанию {DEFAULT_KEY_PATH}")
    parser.set_defaults(_run=run)


def run(args: argparse.Namespace) -> int:
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
