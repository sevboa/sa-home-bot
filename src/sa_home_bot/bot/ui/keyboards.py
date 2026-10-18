from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

BACK_TEXT = "⬅️ Назад"
REFRESH_TEXT = "🔄 Обновить"


def button(text: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def markup(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def single_button(text: str, callback_data: str) -> InlineKeyboardMarkup:
    return markup([button(text, callback_data)])


def back_row(callback_data: str, text: str = BACK_TEXT) -> list[InlineKeyboardButton]:
    return [button(text, callback_data)]


def refresh_row(callback_data: str) -> list[InlineKeyboardButton]:
    return [button(REFRESH_TEXT, callback_data)]
