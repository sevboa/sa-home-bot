"""/scan_now — форс-скан (скрыт из меню, дублирует динамическую кнопку под /status)."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import actions, commands
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.db.store import Store

router = Router(name="control")


@router.message(Command(commands.SCAN_NOW.name))
async def cmd_scan_now(message: Message, store: Store, node_link: ServiceLink) -> None:
    await message.answer(await actions.run_action(store, node_link, "monitor", "scan_now"))
