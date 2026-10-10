"""/away, /back и кнопки напоминания — «Альфред в городе» (Этап 51, bot/away.py).

Только владельцу (``*`` в allowed_commands): ``/away <срок> [причина]``,
``/away +1ч`` (продлить), ``/back``, ``/away`` без аргументов — статус. Кнопки
напоминания за 15 минут до срока — ``away:ext:<минуты>`` и ``away:back``.
Это служебные команды: работают и пока Альфред в городе.
"""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands
from sa_home_bot.bot.away import (
    CALLBACK_PREFIX,
    CB_BACK,
    CB_EXTEND,
    SET_BY_ADMIN,
    AwayError,
    AwayService,
    local_tz,
    parse_away_args,
    parse_extend,
    status_text,
)
from sa_home_bot.bot.away_return import AwayRunner
from sa_home_bot.bot.middlewares import DENIED_TEXT
from sa_home_bot.config import Settings
from sa_home_bot.people import claims as people_claims
from sa_home_bot.people.book import PeopleBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="away")

CLAMPED_TEXT = " Срок урезан до потолка: возвращение не позже чем через 24 часа."


async def _owner_tz(away: AwayService, user):
    """Часовой пояс владельца для «до 23:00»: из его карточки (Этап 58),
    иначе — пояс сервера."""
    if user is not None:
        people = PeopleBook(await away.store.person_claims(), [])
        tz = people.value(user.id, people_claims.FIELD_TIMEZONE)
        if tz:
            try:
                return ZoneInfo(tz)
            except ZoneInfoNotFoundError:
                pass
    return local_tz()


def _is_owner(subscription: Subscription | None) -> bool:
    return subscription is not None and not subscription.broken and subscription.is_owner


@router.message(Command(commands.AWAY.name))
async def cmd_away(
    message: Message,
    command: CommandObject,
    config: Settings,
    away: AwayService | None = None,
    subscription: Subscription | None = None,
) -> None:
    if away is None:
        return
    if not _is_owner(subscription):
        await message.answer(DENIED_TEXT)
        return
    args = (command.args or "").strip()
    tz = await _owner_tz(away, message.from_user)
    now = away.now()
    try:
        if not args:
            await message.answer(status_text(await away.load(), now, tz))
        elif args.startswith("+"):
            state = await away.extend(parse_extend(args))
            await message.answer("Продлил. " + status_text(state, away.now(), tz))
        else:
            until, reason = parse_away_args(args, now, tz)
            state, clamped = await away.start(until, set_by=SET_BY_ADMIN, reason=reason)
            await message.answer(
                "Альфред спустился в город. "
                + status_text(state, away.now(), tz)
                + (CLAMPED_TEXT if clamped else "")
            )
    except AwayError as exc:
        await message.answer(str(exc))


@router.message(Command(commands.BACK.name))
async def cmd_back(
    message: Message,
    away: AwayService | None = None,
    away_runner: AwayRunner | None = None,
    subscription: Subscription | None = None,
) -> None:
    if away is None:
        return
    if not _is_owner(subscription):
        await message.answer(DENIED_TEXT)
        return
    await message.answer(await _do_back(away, away_runner))


async def _do_back(away: AwayService, runner: AwayRunner | None) -> str:
    try:
        state, immediate = await away.back()
    except AwayError as exc:
        return str(exc)
    if immediate:
        return "Альфред вернулся в замок. Ждать ответа некому."
    if runner is not None:
        runner.kick()
    return (
        f"Альфред возвращается в замок. Чатов, ждущих ответа: {len(state.pending)} — "
        "разберёт по одному, как только модель будет готова."
    )


@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
async def cb_away(
    callback: CallbackQuery,
    config: Settings,
    away: AwayService | None = None,
    away_runner: AwayRunner | None = None,
    subscription: Subscription | None = None,
) -> None:
    if away is None:
        await callback.answer()
        return
    if not _is_owner(subscription):
        await callback.answer("⛔️ Недоступно", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action == CB_EXTEND and len(parts) > 2 and parts[2].isdigit():
            state = await away.extend(parse_extend(f"{int(parts[2])}м"))
            text = "Продлил. " + status_text(
                state, away.now(), await _owner_tz(away, callback.from_user)
            )
        elif action == CB_BACK:
            text = await _do_back(away, away_runner)
        else:
            await callback.answer()
            return
    except AwayError as exc:
        text = str(exc)
    await callback.answer()
    if callback.message is not None:
        try:
            await callback.message.edit_text(text, reply_markup=None)
        except TelegramBadRequest as exc:
            log.info("away: напоминание не поправить: %s", exc)
