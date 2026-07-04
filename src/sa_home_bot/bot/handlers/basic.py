"""Базовые хендлеры: /start, /help, /ping, /whoami (универсальные)."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="basic")


def _build_help(subscription: Subscription | None) -> str:
    lines = ["<b>Доступные команды</b>", ""]
    for cmd in commands.UNIVERSAL_COMMANDS:
        lines.append(f"/{cmd.name} — {cmd.description}")
    available_control = [
        cmd
        for cmd in commands.MENU_CONTROL_COMMANDS
        if subscription is not None and subscription.allows_command(cmd.name)
    ]
    if available_control:
        lines.append("")
        for cmd in available_control:
            lines.append(f"/{cmd.name} — {cmd.description}")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, subscription: Subscription | None = None) -> None:
    await message.answer(
        "👋 Я бот-сторож домашней машины — слежу за температурой CPU и дисков.\n\n"
        + _build_help(subscription)
    )


@router.message(Command(commands.HELP.name))
async def cmd_help(message: Message, subscription: Subscription | None = None) -> None:
    await message.answer(_build_help(subscription))


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
