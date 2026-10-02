"""Опись вещей (/items) и кнопки карточки сюжетного предмета (Этап 49.3,
bot/interactives/items.py).

callback_data «it:<id предмета>:<кнопка>»; права — Interactives.
handle_item_click по from_user.id (кнопка работает только у владельца).
У «Проклятой радиостанции» кнопка ставит старую станцию или убирает её —
это и есть переключатель картавости Альфреда.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands
from sa_home_bot.bot.interactives import engine
from sa_home_bot.bot.interactives.engine import Interactives

log = logging.getLogger(__name__)

router = Router(name="items")


@router.callback_query(F.data.startswith(f"{engine.ITEM_CALLBACK_PREFIX}:"))
async def cb_item(callback: CallbackQuery, interactives: Interactives | None = None) -> None:
    parsed = engine.parse_item_callback(callback.data)
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
    item_id, button = parsed
    answer, markup, caption = await interactives.handle_item_click(
        message.chat.id,
        callback.from_user.id,
        item_id,
        button,
        message_id=message.message_id,
        message_thread_id=getattr(message, "message_thread_id", None),
    )
    await callback.answer(answer)
    if markup is None:
        return
    try:
        if caption is not None and getattr(message, "photo", None):
            await message.edit_caption(caption=caption, reply_markup=markup)
        elif caption is not None:
            await message.edit_text(caption, reply_markup=markup)
        else:
            await message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest as exc:
        # Итог уже записан (речь переключена) — правка карточки лишь косметика.
        log.info("items: карточку не поправить (chat=%s): %s", message.chat.id, exc)


@router.message(Command(commands.ITEMS.name))
async def cmd_items(message: Message, interactives: Interactives | None = None) -> None:
    if interactives is None or message.from_user is None:
        return
    text, markup = await interactives.inventory(message.from_user.id)
    await message.answer(text, reply_markup=markup)
