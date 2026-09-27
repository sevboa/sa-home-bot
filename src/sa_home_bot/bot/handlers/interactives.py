"""Кнопки интерактивов и тумблер /interactives (Этап 47, bot/interactives).

callback_data «ia:<сценарий>:<кнопка>» несёт только сценарий и кнопку —
состояние сцены в app_state бота. Права проверяет Interactives.handle_click
по from_user.id (форма — только тому гостю, чья сцена), а не по чату.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands
from sa_home_bot.bot.interactives import engine
from sa_home_bot.bot.interactives.engine import Interactives

log = logging.getLogger(__name__)

router = Router(name="interactives")


@router.callback_query(F.data.startswith(f"{engine.CALLBACK_PREFIX}:"))
async def cb_interactive(
    callback: CallbackQuery, interactives: Interactives | None = None
) -> None:
    parsed = engine.parse_callback(callback.data)
    message = callback.message
    if (
        parsed is None
        or interactives is None
        or callback.from_user is None
        or message is None
        or message.chat is None
    ):
        await callback.answer()
        return
    scenario_id, button = parsed
    answer, new_text, drop_keyboard = await interactives.handle_click(
        message.chat.id, callback.from_user.id, scenario_id, button
    )
    alert = button == engine.BTN_EXIT
    await callback.answer(answer, show_alert=alert)
    try:
        if new_text is not None:
            await message.edit_text(new_text, reply_markup=None)
        elif drop_keyboard:
            await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest as exc:
        # «message is not modified», удалённое сообщение и т.п. — итог уже
        # записан в app_state, правка формы лишь косметика.
        log.info("interactives: форму не поправить (chat=%s): %s", message.chat.id, exc)


@router.message(Command(commands.INTERACTIVES.name))
async def cmd_interactives(
    message: Message, command: CommandObject, interactives: Interactives | None = None
) -> None:
    if interactives is None:
        return
    arg = (command.args or "").strip().lower()
    chat_id = message.chat.id
    if arg in ("off", "выкл", "нет"):
        await interactives.set_opted_out(chat_id, True)
        await message.answer(engine.OPT_OUT_TEXT)
        return
    if arg in ("", "on", "вкл", "да"):
        if arg == "" and not await interactives.is_opted_out(chat_id):
            await message.answer(
                "Интерактивы в этом чате включены. Выключить — /interactives off."
            )
            return
        await interactives.set_opted_out(chat_id, False)
        await message.answer(engine.OPT_IN_TEXT)
        return
    await message.answer("Использование: /interactives on | off")
