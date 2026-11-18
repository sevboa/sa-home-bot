from __future__ import annotations

import argparse
import asyncio
import html
import sys

from sa_home_bot.config import Settings

DESCRIPTION = "Тишина видеокарты: status, set [минуты], off."


def add_silent_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("silent", help="тишина видеокарты", description=DESCRIPTION)
    sub = parser.add_subparsers(dest="silent_command", required=True)

    set_p = sub.add_parser("set", help="включить тишину")
    set_p.add_argument(
        "minutes", nargs="?", type=int, default=None, help="минуты (без числа — бессрочно)"
    )
    sub.add_parser("off", help="снять тишину")
    sub.add_parser("status", help="показать состояние")
    for p in (set_p, sub.choices["off"]):
        p.add_argument(
            "--by",
            choices=("claude", "admin"),
            default="claude",
            help="кто включил (по умолчанию claude — владельцу уйдёт сообщение)",
        )
    parser.set_defaults(_run=run)


def run(args: argparse.Namespace, settings: Settings) -> int:
    return asyncio.run(_run(args, settings))


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    from sa_home_bot.bot.away import SET_BY_CLAUDE, AwayService
    from sa_home_bot.bot.gpu_silence import away_for_silence, start_silence, until_text
    from sa_home_bot.db.connection import Database
    from sa_home_bot.db.store import Store
    from sa_home_bot.silence import SilenceService

    path = settings.database.path
    if not path.exists():
        print(f"БД бота не найдена: {path}", file=sys.stderr)
        return 2
    db = Database(path)
    await db.open()
    try:
        store = Store(db)
        silence = SilenceService(store)
        away = AwayService(store, settings)
        by = getattr(args, "by", "claude")
        notify = by == SET_BY_CLAUDE

        def show(text: str) -> None:
            print(html.unescape(text))

        if args.silent_command == "status":
            state = await silence.load()
            show(f"🔇 Тишина видеокарты {until_text(state)}." if state else "🔈 Тишины нет.")
            return 0
        if args.silent_command == "set":
            try:
                state = await start_silence(silence, away, args.minutes, set_by=by)
            except ValueError as exc:
                print(html.unescape(str(exc)), file=sys.stderr)
                return 1
            text = f"🔇 Тишина видеокарты {until_text(state)}. Альфред в городе на это время."
            show(text)
            if notify:
                await away.queue_notice("Claude включил тишину видеокарты. " + text)
            return 0
        was = await silence.off()
        text = "Тишина снята." if was else "Тишины и так нет."
        if was and await away_for_silence(away):
            await away.back()
        show("🔈 " + text)
        if notify:
            await away.queue_notice("Claude снял тишину видеокарты. " + text)
        return 0
    finally:
        await db.close()
