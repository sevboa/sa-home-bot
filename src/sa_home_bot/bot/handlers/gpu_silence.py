from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.bot.away import (
    MAX_AWAY,
    SET_BY_ADMIN,
    AwayError,
    AwayService,
    local_tz,
)
from sa_home_bot.bot.away_return import AwayRunner
from sa_home_bot.bot.handlers.away import do_back
from sa_home_bot.bot.middlewares import DENIED_TEXT
from sa_home_bot.silence import SilenceService, SilenceState
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="gpu_silence")

SILENCE_AWAY_REASON = "тишина видеокарты"

UNKNOWN_TEXT = (
    "Не понял. /silent — до /silent off, /silent 30 — на 30 минут, /silent off — снять, "
    "/silent status — состояние."
)


def _allowed(subscription: Subscription | None) -> bool:
    return subscription is not None and (
        subscription.is_owner or subscription.allows_command(commands.SILENT.name)
    )


def _until_text(state: SilenceState) -> str:
    if state.until is None:
        return "до /silent off"
    return "до " + state.until.astimezone(local_tz()).strftime("%d.%m %H:%M")


@router.message(Command(commands.SILENT.name))
async def cmd_silent(
    message: Message,
    command: CommandObject,
    silence: SilenceService | None = None,
    away: AwayService | None = None,
    away_runner: AwayRunner | None = None,
    subscription: Subscription | None = None,
) -> None:
    if silence is None:
        return
    if not _allowed(subscription):
        await message.answer(DENIED_TEXT)
        return
    args = (command.args or "").strip().lower()
    if args == "off":
        was = await silence.off()
        text = "🔈 Тишина снята." if was else "🔈 Тишины и так нет."
        current = await away.load() if away is not None else None
        if away is not None and current is not None and current.reason == SILENCE_AWAY_REASON:
            text += " " + await do_back(away, away_runner)
        await message.answer(text)
        return
    if args == "status":
        state = await silence.load()
        text = f"🔇 Тишина видеокарты {_until_text(state)}." if state else "🔈 Тишины нет."
        await message.answer(text)
        return
    if args and not (args.isdigit() and int(args) > 0):
        await message.answer(UNKNOWN_TEXT)
        return
    state = await silence.start(int(args) if args else None, set_by=str(subscription.chat_id))
    if away is not None:
        target = state.until or away.now() + MAX_AWAY
        current = await away.load()
        try:
            if current is None:
                await away.start(target, set_by=SET_BY_ADMIN, reason=SILENCE_AWAY_REASON)
            elif current.until < target:
                await away.extend(target - current.until)
        except AwayError:
            pass
    await message.answer(
        f"🔇 Тишина видеокарты {_until_text(state)}. Альфред в городе на это время."
    )
