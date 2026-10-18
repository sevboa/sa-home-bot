from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import actions, commands, status_view
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.db.store import Store

router = Router(name="control")


@router.message(Command(commands.STATS.name))
async def cmd_stats(message: Message, node_link: ServiceLink) -> None:
    await message.answer(
        await status_view.build_stats_text(node_link, dst=status_view.monitor_dst(None))
    )


@router.message(Command(commands.DOWNTIME.name))
async def cmd_downtime(message: Message, node_link: ServiceLink) -> None:
    text, keyboard = await status_view.build_downtime_page(node_link)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command(commands.SCAN_NOW.name))
async def cmd_scan_now(message: Message, store: Store, node_link: ServiceLink) -> None:
    await message.answer(await actions.run_action(store, node_link, "monitor", "scan_now"))
