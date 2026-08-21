"""Меню команд Telegram: что видит чат в списке команд бота.

Меню — скилы роя первого уровня: динамические команды-приложения из describe
службы apps + «Сводка роя», затем универсальные. Перестраивается при
(пере)подключении к службе apps — новое приложение на любой ноде = новая
команда в меню без изменения кода бота.

Живёт отдельно от `bot/setup.py` (где собирается Dispatcher) по простой
причине: меню правит не только старт, но и хендлеры — приватный вход даёт
меню только что впущенному гостю и убирает у выставленного
(`bot/handlers/invites.py`). Импортируй хендлер setup.py — получился бы цикл,
ведь setup.py сам подключает хендлеры.

`set_my_commands` — это UX, не защита: команду можно отправить и текстом,
проверяют её middleware (AUTHORIZATION.md §6).
"""

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
    """Меню чата: скилы (приложения + ноды) по правам, затем универсальные."""
    menu = [
        BotCommand(command=action.id, description=f"{action.title}: состояние и веб-морда")
        for action in app_actions
        # Параметризованные (start/stop/restart с обязательным "какое
        # приложение") — только кнопки на карточке конкретного приложения
        # (apps_view.run_app_skill), не голые команды меню — живой баг
        # 2026-07-18: /start/stop/restart лезли в меню без указания, какое
        # приложение, и падали "нет такого приложения: ''".
        if not action.params
        and action.id not in apps_view.HIDDEN_MENU_APP_IDS
        and subscription.allows_action(action.id, apps_view.APPS_SERVICE)
    ]
    menu += [
        _to_bot_command(c)
        for c in commands.MENU_CONTROL_COMMANDS
        if subscription.allows_command(c.right or c.name)
    ]
    menu += [_to_bot_command(c) for c in commands.MENU_UNIVERSAL_COMMANDS]
    return menu


async def refresh_chat_menu(
    bot: Bot,
    chat_id: int,
    subscription: Subscription | None,
    app_actions: Sequence[ActionSpec] = (),
) -> None:
    """Перестроить меню одного чата. ``None`` — чат больше не наш: меню
    убирается совсем, чтобы у выставленного гостя не осталось кнопок, которые
    всё равно не сработают."""
    scope = BotCommandScopeChat(chat_id=chat_id)
    try:
        if subscription is None:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(build_menu_commands(subscription, app_actions), scope=scope)
    except Exception as exc:  # noqa: BLE001 — меню не повод ронять обработку
        log.warning("Не удалось обновить меню chat_id=%s: %s", chat_id, exc)


async def set_bot_commands(
    bot: Bot,
    book: SubscriptionBook,
    app_actions: Sequence[ActionSpec] = (),
) -> None:
    """default scope — пусто; per-chat — скилы по правам + универсальные.

    Пустой default — часть приватного входа (AUTHORIZATION.md §10). Меню
    команд Telegram показывает всякому, кто открыл чат с ботом, ещё до
    первого сообщения: оставь там что-то — список умений всё равно был бы
    виден постороннему. Свои ничего не теряют: у каждого подписного чата меню
    своё, per-chat scope перекрывает default.
    """
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
    except Exception as exc:  # noqa: BLE001 — сетевой блип при старте не должен ронять бот
        log.warning("Не удалось очистить меню по умолчанию: %s", exc)

    for sub in book.all():
        if sub.broken:
            continue
        await refresh_chat_menu(bot, sub.chat_id, sub, app_actions)
