from __future__ import annotations

from collections.abc import Callable

from aiogram.types import InlineKeyboardButton


def nav_row(
    offset: int, size: int, total: int, cb: Callable[[int], str]
) -> list[InlineKeyboardButton]:
    buttons: list[InlineKeyboardButton] = []
    if offset > 0:
        buttons.append(InlineKeyboardButton(text="⬅️", callback_data=cb(max(0, offset - size))))
    if offset + size < total:
        buttons.append(InlineKeyboardButton(text="➡️", callback_data=cb(offset + size)))
    return buttons


def clamp_offset(offset: int, size: int, total: int) -> int:
    if total == 0:
        return 0
    last_page = ((total - 1) // size) * size
    return min(max(offset, 0), last_page)
