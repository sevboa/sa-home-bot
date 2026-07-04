"""Сборка Bot/Dispatcher, цепочка middleware, меню команд per-chat."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from sa_home_bot.bot import commands
from sa_home_bot.bot.handlers import basic, control, power, stats, status
from sa_home_bot.bot.middlewares import (
    AuthorizationMiddleware,
    CallbackAuthorizationMiddleware,
)
from sa_home_bot.subscriptions.book import SubscriptionBook

log = logging.getLogger(__name__)


def build_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher(book: SubscriptionBook) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AuthorizationMiddleware(book))
    dp.callback_query.middleware(CallbackAuthorizationMiddleware(book))
    dp.include_router(basic.router)
    dp.include_router(status.router)
    dp.include_router(stats.router)
    dp.include_router(control.router)
    dp.include_router(power.router)
    return dp


def _to_bot_command(cmd: commands.Command) -> BotCommand:
    return BotCommand(command=cmd.name, description=cmd.description)


async def set_bot_commands(bot: Bot, book: SubscriptionBook) -> None:
    """default scope — универсальные; per-chat — универсальные + разрешённые управляющие."""
    universal = [_to_bot_command(c) for c in commands.UNIVERSAL_COMMANDS]
    try:
        await bot.set_my_commands(universal, scope=BotCommandScopeDefault())
    except Exception as exc:  # noqa: BLE001 — сетевой блип при старте не должен ронять бот
        log.warning("Не удалось задать меню по умолчанию: %s", exc)

    for sub in book.all():
        if sub.broken:
            continue
        chat_cmds = list(universal) + [
            _to_bot_command(c)
            for c in commands.MENU_CONTROL_COMMANDS
            if sub.allows_command(c.name)
        ]
        try:
            await bot.set_my_commands(
                chat_cmds, scope=BotCommandScopeChat(chat_id=sub.chat_id)
            )
        except Exception as exc:  # noqa: BLE001 — не критично для запуска
            log.warning("Не удалось задать меню для chat_id=%s: %s", sub.chat_id, exc)
