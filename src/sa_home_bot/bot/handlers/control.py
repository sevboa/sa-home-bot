"""/scan_now — поставить форс-скан в очередь."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.jobs.scan import SensorScanJob
from sa_home_bot.worker.queue import DedupQueue

router = Router(name="control")


@router.message(Command(commands.SCAN_NOW.name))
async def cmd_scan_now(message: Message, queue: DedupQueue) -> None:
    queued = await queue.put(SensorScanJob())
    if queued:
        await message.answer("🔄 Скан поставлен в очередь.")
    else:
        await message.answer("⏳ Скан уже в очереди — дождитесь результата.")
