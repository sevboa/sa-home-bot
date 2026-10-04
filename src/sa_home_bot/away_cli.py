"""`sa-home-bot away set|extend|back|status` — «Альфред в городе» из консоли (Этап 51).

Для Claude и владельца на сервере: перед обслуживанием/занятием mycraft
отправить Альфреда в город, после — вернуть. Пишет ту же запись
``app_state["alfred_away"]`` в БД бота, что и Telegram-команды (bot/away.py);
бот читает её на каждом входящем, поэтому перезапуск не нужен. Изменение,
сделанное от имени Claude (по умолчанию), бот сообщает владельцу в Telegram —
CLI кладёт сообщение в очередь, бот отправляет его на ближайшем проходе
(до минуты).

Схему БД CLI не трогает (её ведёт бот), нет файла БД — отказ, а не создание
пустой.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import sys

from sa_home_bot.config import Settings

DESCRIPTION = (
    "Альфред в городе: окно обслуживания mycraft. Срок: 3h, 1h30m, 3ч, до 23:00, "
    "завтра 10:00 (время — по поясу сервера)."
)


def add_away_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("away", help="Альфред в городе (окно обслуживания)",
                                   description=DESCRIPTION)
    sub = parser.add_subparsers(dest="away_command", required=True)

    set_p = sub.add_parser("set", help="отправить Альфреда в город")
    set_p.add_argument("duration", nargs="+", help="срок: 3h | 3ч | до 23:00 | завтра 10:00")
    set_p.add_argument("--reason", default="", help="причина (гостям не видна)")
    extend_p = sub.add_parser("extend", help="продлить («задерживается»)")
    extend_p.add_argument("duration", help="на сколько: 1h, 30м")
    sub.add_parser("back", help="вернуть Альфреда")
    sub.add_parser("status", help="показать состояние")
    for p in (set_p, extend_p, sub.choices["back"]):
        p.add_argument("--by", choices=("claude", "admin"), default="claude",
                       help="кто включил (по умолчанию claude — владельцу уйдёт сообщение)")
    parser.set_defaults(_run=run)


def run(args: argparse.Namespace, settings: Settings) -> int:
    return asyncio.run(_run(args, settings))


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    from sa_home_bot.bot.away import (  # noqa: PLC0415 — тяжёлые импорты только для этой команды
        SET_BY_CLAUDE,
        AwayError,
        AwayService,
        local_tz,
        parse_away_args,
        parse_extend,
        status_text,
    )
    from sa_home_bot.db.connection import Database  # noqa: PLC0415
    from sa_home_bot.db.store import Store  # noqa: PLC0415

    path = settings.database.path
    if not path.exists():
        print(f"БД бота не найдена: {path}", file=sys.stderr)
        return 2
    db = Database(path)
    await db.open()
    try:
        service = AwayService(Store(db), settings)
        tz = local_tz()
        by = getattr(args, "by", "claude")
        notify = by == SET_BY_CLAUDE

        def show(text: str) -> None:
            print(html.unescape(text))

        try:
            if args.away_command == "status":
                show(status_text(await service.load(), service.now(), tz))
                return 0
            if args.away_command == "set":
                until, inline_reason = parse_away_args(" ".join(args.duration), service.now(), tz)
                reason = args.reason or inline_reason
                state, clamped = await service.start(until, set_by=by, reason=reason)
                text = status_text(state, service.now(), tz)
                if clamped:
                    text += " Срок урезан до потолка 24 ч."
                show(text)
                if notify:
                    await service.queue_notice("Claude отправил Альфреда в город. " + text)
            elif args.away_command == "extend":
                state = await service.extend(parse_extend(args.duration))
                text = status_text(state, service.now(), tz)
                show(text)
                if notify:
                    await service.queue_notice("Claude продлил отъезд Альфреда. " + text)
            else:  # back
                state, immediate = await service.back()
                text = (
                    "Альфред вернулся, ждать некому."
                    if immediate
                    else f"Альфред возвращается, в очереди чатов: {len(state.pending)}."
                )
                show(text)
                if notify:
                    await service.queue_notice("Claude вернул Альфреда. " + text)
        except AwayError as exc:
            print(html.unescape(str(exc)), file=sys.stderr)
            return 1
        return 0
    finally:
        await db.close()
