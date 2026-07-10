"""Базовые хендлеры: /start, /help, /ping, /whoami (универсальные)."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from sa_home_bot import __version__
from sa_home_bot.bot import apps_view, commands
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.proto.messages import ActionSpec
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="basic")

ABOUT_LINE = f"ℹ️ sa-home-bot v{__version__}"


def build_help(
    subscription: Subscription | None,
    app_actions: Sequence[ActionSpec] = (),
) -> str:
    """Скилы роя первого уровня, затем универсальные команды и about."""
    skills = [
        f"/{action.id} — {action.title}: состояние и веб-морда"
        for action in app_actions
        if subscription is not None
        and subscription.allows_action(action.id, apps_view.APPS_SERVICE)
    ]
    skills += [
        f"/{cmd.name} — {cmd.description}"
        for cmd in commands.MENU_CONTROL_COMMANDS
        if subscription is not None and subscription.allows_command(cmd.name)
    ]
    lines = ["<b>Доступные команды</b>", ""]
    if skills:
        lines += skills
        lines.append("")
    lines += [f"/{cmd.name} — {cmd.description}" for cmd in commands.UNIVERSAL_COMMANDS]
    lines += ["", ABOUT_LINE]
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    apps_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    await message.answer(
        "👋 Я бот роя домашних машин: скилы роя — прямо в меню команд.\n\n"
        + build_help(subscription, await apps_link.actions())
    )


@router.message(Command(commands.HELP.name))
async def cmd_help(
    message: Message,
    apps_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    await message.answer(build_help(subscription, await apps_link.actions()))


@router.message(Command(commands.PING.name))
async def cmd_ping(message: Message) -> None:
    await message.answer("🏓 pong")


@router.message(Command(commands.WHOAMI.name))
async def cmd_whoami(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "—"
    chat_id = message.chat.id
    chat_type = escape(message.chat.type)
    await message.answer(
        f"<b>user_id:</b> <code>{user_id}</code>\n"
        f"<b>chat_id:</b> <code>{chat_id}</code>\n"
        f"<b>chat type:</b> {chat_type}"
    )
