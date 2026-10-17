from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def markup(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def single_button(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return markup([button(text, callback_data)])
