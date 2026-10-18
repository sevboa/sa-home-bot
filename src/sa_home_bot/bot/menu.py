from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from sa_home_bot.bot import apps_view, commands
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)


def _to_bot_command(cmd: commands.Command) -> BotCommand:
    return BotCommand(command=cmd.name, description=cmd.description)


def build_menu_commands(
    subscription: Subscription,
    app_actions: Sequence[ActionSpec] = (),
) -> list[BotCommand]:
    menu = [
        BotCommand(command=action.id, description=f"{action.title}: состояние и веб-морда")
        for action in apps_view.visible_skill_actions(subscription, app_actions)
    ]
    menu += [_to_bot_command(c) for c in commands.menu_control_commands_for(subscription)]
    menu += [_to_bot_command(c) for c in commands.MENU_UNIVERSAL_COMMANDS]
    return menu


async def refresh_chat_menu(
    bot: Bot,
    chat_id: int,
    subscription: Subscription | None,
    app_actions: Sequence[ActionSpec] = (),
) -> None:
    scope = BotCommandScopeChat(chat_id=chat_id)
    try:
        if subscription is None:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(build_menu_commands(subscription, app_actions), scope=scope)
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось обновить меню chat_id=%s: %s", chat_id, exc)


async def set_bot_commands(
    bot: Bot,
    book: SubscriptionBook,
    app_actions: Sequence[ActionSpec] = (),
) -> None:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception as exc:  # noqa: BLE001
        log.warning("Не удалось очистить меню по умолчанию: %s", exc)

    for sub in book.all():
        if sub.broken:
            continue
        await refresh_chat_menu(bot, sub.chat_id, sub, app_actions)
