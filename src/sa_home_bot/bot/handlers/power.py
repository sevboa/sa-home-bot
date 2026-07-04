"""/downtime — последние отключения машины из журнала загрузок (`last`)."""

from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.domain.render import render_downtime
from sa_home_bot.sensors.power import read_power_events_sync

router = Router(name="power")


@router.message(Command(commands.DOWNTIME.name))
async def cmd_downtime(message: Message) -> None:
    loop = asyncio.get_running_loop()
    events = await loop.run_in_executor(None, read_power_events_sync, 10)
    await message.answer(render_downtime(events))
