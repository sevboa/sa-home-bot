"""/stats — сводка прогонов сканера (скрыта из меню, доступна кнопкой под /status)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands, status_view
from sa_home_bot.bot.monitor_link import MonitorLink

router = Router(name="stats")


@router.message(Command(commands.STATS.name))
async def cmd_stats(message: Message, link: MonitorLink) -> None:
    await message.answer(await status_view.build_stats_text(link))
