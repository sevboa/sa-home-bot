"""/scan_now — форс-скан (скрыт из меню, доступен кнопкой под /status)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands, status_view
from sa_home_bot.worker.queue import DedupQueue

router = Router(name="control")


@router.message(Command(commands.SCAN_NOW.name))
async def cmd_scan_now(message: Message, queue: DedupQueue) -> None:
    await message.answer(await status_view.build_scan_text(queue))
