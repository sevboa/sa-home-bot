"""/downtime — история отключений (скрыта из меню, доступна кнопкой под /status)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands, status_view
from sa_home_bot.bot.service_link import ServiceLink

router = Router(name="power")


@router.message(Command(commands.DOWNTIME.name))
async def cmd_downtime(message: Message, node_link: ServiceLink) -> None:
    text, keyboard = await status_view.build_downtime_page(node_link)
    await message.answer(text, reply_markup=keyboard)
