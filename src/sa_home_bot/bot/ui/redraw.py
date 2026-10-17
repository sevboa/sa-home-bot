from __future__ import annotations

import contextlib
from typing import Any

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup


async def redraw(
    callback: CallbackQuery,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
    **edit_options: Any,
) -> None:
    if callback.message is None:
        return
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard, **edit_options)
