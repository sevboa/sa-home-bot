"""Точка входа CLI: разбор аргументов, загрузка Settings, запуск приложения."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from sa_home_bot import __version__
from sa_home_bot.config import Settings
from sa_home_bot.utils.logging import configure_logging

log = logging.getLogger(__name__)

# Windows: os.execv не заменяет образ процесса (новый PID, обёртка службы
# сочтёт ноду умершей) — само-рестарт там делается выходом с этим кодом,
# перезапуск выполняет обёртка (WinSW <onfailure action="restart"/>) или человек.
RESTART_EXIT_CODE = 10


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sa-home-bot",
        description="Личный бот-сторож температуры CPU и дисков домашней машины.",
    )
    parser.add_argument("--config", "-c", default=None, help="путь к config.toml")
    parser.add_argument(
        "--service",
        choices=("bot", "monitor", "apps", "torrents", "node"),
        default="bot",
        help="какую службу запустить: telegram-бот (по умолчанию), "
        "монитор датчиков, адаптер приложений, адаптер торрент-клиента "
        "или сервис ноды (супервизор)",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="загрузить и напечатать разобранный конфиг, затем выйти",
    )
    parser.add_argument("--version", "-V", action="version", version=f"sa-home-bot {__version__}")
    return parser


def _redacted(settings: Settings) -> dict:
    data = settings.model_dump(mode="json")
    token = data.get("telegram", {}).get("token", "")
    if token:
        data["telegram"]["token"] = token[:4] + "…(скрыто)"
    if data.get("torrents", {}).get("qbittorrent_password"):
        data["torrents"]["qbittorrent_password"] = "…(скрыто)"
    return data


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        settings = Settings.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 2

    if args.check_config:
        import json

        print(json.dumps(_redacted(settings), ensure_ascii=False, indent=2))
        return 0

    configure_logging(settings.logging.level, settings.logging.format)

    # Импорт здесь, чтобы --check-config не тянул тяжёлые зависимости.
    if args.service == "node":
        from sa_home_bot.node.app import run_node

        # Ноде нужен путь к конфигу — она передаёт его дочерним службам.
        coro = run_node(settings, config_path=args.config)
    elif args.service == "monitor":
        from sa_home_bot.monitor.app import run_monitor

        coro = run_monitor(settings)
    elif args.service == "apps":
        from sa_home_bot.apps.app import run_apps

        coro = run_apps(settings)
    elif args.service == "torrents":
        from sa_home_bot.torrents.app import run_torrents

        coro = run_torrents(settings)
    else:
        from sa_home_bot.app import run

        coro = run(settings)

    try:
        restart = asyncio.run(coro)
    except KeyboardInterrupt:
        return 0
    if restart:
        # run_node вернул True (запрошен само-рестарт «restart_node»): чистый
        # останов уже прошёл, заменяем образ процесса на себя же — тот же PID,
        # работает и под systemd (Restart= не нужен), и вручную в терминале.
        if sys.platform == "win32":
            log.info("Само-рестарт: выход с кодом %d, перезапуск — за обёрткой "
                     "службы (WinSW) или вручную", RESTART_EXIT_CODE)
            return RESTART_EXIT_CODE
        log.info("Само-рестарт: %s", sys.argv)
        os.execv(sys.argv[0], sys.argv)
    return 0
