"""Сборка Bot/Dispatcher, цепочка middleware, меню команд per-chat.

Меню — скилы роя первого уровня: динамические команды-приложения из describe
службы apps + «Управление нодами», затем универсальные. Меню перестраивается
при (пере)подключении к службе apps — новое приложение на любой ноде = новая
команда в меню без изменения кода бота.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from sa_home_bot.bot import apps_view, commands
from sa_home_bot.bot.handlers import (
    apps,
    basic,
    control,
    node,
    node_links,
    power,
    stats,
    status,
    wake,
)
from sa_home_bot.bot.middlewares import (
    AuthorizationMiddleware,
    CallbackAuthorizationMiddleware,
)
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)


def build_bot(token: str) -> Bot:
    return Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def build_dispatcher(book: SubscriptionBook) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(AuthorizationMiddleware(book))
    dp.callback_query.middleware(CallbackAuthorizationMiddleware(book))
    dp.include_router(basic.router)
    # wake раньше status: его callback «st:wake» точечный, а on_status_action
    # ловит весь префикс «st:».
    dp.include_router(wake.router)
    dp.include_router(status.router)
    dp.include_router(stats.router)
    dp.include_router(control.router)
    dp.include_router(power.router)
    dp.include_router(node.router)
    # node_links до apps: ловит фиксированные префиксы /node_* и /svc_*.
    dp.include_router(node_links.router)
    # apps последним: ловит динамические команды-скилы, остальное игнорирует.
    dp.include_router(apps.router)
    return dp


def _to_bot_command(cmd: commands.Command) -> BotCommand:
    return BotCommand(command=cmd.name, description=cmd.description)


def build_menu_commands(
    subscription: Subscription,
    app_actions: Sequence[ActionSpec] = (),
) -> list[BotCommand]:
    """Меню чата: скилы (приложения + ноды) по правам, затем универсальные."""
    menu = [
        BotCommand(command=action.id, description=f"{action.title}: состояние и веб-морда")
        for action in app_actions
        if subscription.allows_action(action.id, apps_view.APPS_SERVICE)
    ]
    menu += [
        _to_bot_command(c)
        for c in commands.MENU_CONTROL_COMMANDS
        if subscription.allows_command(c.right or c.name)
    ]
    menu += [_to_bot_command(c) for c in commands.UNIVERSAL_COMMANDS]
    return menu


async def set_bot_commands(
    bot: Bot,
    book: SubscriptionBook,
    app_actions: Sequence[ActionSpec] = (),
) -> None:
    """default scope — универсальные; per-chat — скилы по правам + универсальные."""
    universal = [_to_bot_command(c) for c in commands.UNIVERSAL_COMMANDS]
    try:
        await bot.set_my_commands(universal, scope=BotCommandScopeDefault())
    except Exception as exc:  # noqa: BLE001 — сетевой блип при старте не должен ронять бот
        log.warning("Не удалось задать меню по умолчанию: %s", exc)

    for sub in book.all():
        if sub.broken:
            continue
        try:
            await bot.set_my_commands(
                build_menu_commands(sub, app_actions),
                scope=BotCommandScopeChat(chat_id=sub.chat_id),
            )
        except Exception as exc:  # noqa: BLE001 — не критично для запуска
            log.warning("Не удалось задать меню для chat_id=%s: %s", sub.chat_id, exc)
