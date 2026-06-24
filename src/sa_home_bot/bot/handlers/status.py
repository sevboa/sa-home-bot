"""/status — текущее состояние компонентов из БД."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from sa_home_bot.bot import commands
from sa_home_bot.db.store import Store
from sa_home_bot.domain.models import ALERTING
from sa_home_bot.domain.render import render_state_line

router = Router(name="status")


@router.message(Command(commands.STATUS.name))
async def cmd_status(message: Message, store: Store) -> None:
    states = await store.get_all_states()
    if not states:
        await message.answer("Пока нет данных — сканер ещё не снимал срез.")
        return

    alerting = [s for s in states if s.status == ALERTING]
    header = "🔥 <b>Есть перегрев!</b>" if alerting else "✅ <b>Всё в норме.</b>"
    lines = [header, ""]
    lines.extend(render_state_line(s) for s in states)
    await message.answer("\n".join(lines))
