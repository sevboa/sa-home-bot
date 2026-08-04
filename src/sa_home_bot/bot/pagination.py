"""Общая пагинация постраничных списков в inline-клавиатурах бота.

Вынесено из bot/guests_view.py (было там локальными _nav_row/clamp_offset) —
тот же приём нужен bot/swarm_panel.py и bot/node_view.py (списки нод, умений,
служб), дублировать 20 строк ради «независимости модулей» смысла не было.
"""

from __future__ import annotations

from collections.abc import Callable

from aiogram.types import InlineKeyboardButton


def nav_row(
    offset: int, size: int, total: int, cb: Callable[[int], str]
) -> list[InlineKeyboardButton]:
    """Кнопки «⬅️»/«➡️» для страницы [offset, offset+size) из total элементов.

    ``cb`` строит callback_data по новому offset — вызывающий уже знает свой
    код экрана и постоянные параметры (chat_id, node_id и т.п.).
    """
    buttons: list[InlineKeyboardButton] = []
    if offset > 0:
        buttons.append(InlineKeyboardButton(text="⬅️", callback_data=cb(max(0, offset - size))))
    if offset + size < total:
        buttons.append(InlineKeyboardButton(text="➡️", callback_data=cb(offset + size)))
    return buttons


def clamp_offset(offset: int, size: int, total: int) -> int:
    """Страница могла опустеть (последний элемент убрали) — валидный offset."""
    if total == 0:
        return 0
    last_page = ((total - 1) // size) * size
    return min(max(offset, 0), last_page)
