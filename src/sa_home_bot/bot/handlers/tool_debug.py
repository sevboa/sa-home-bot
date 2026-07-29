"""Кнопка «развернуть/скрыть» под дебаг-сообщением о вызове инструмента.

Само сообщение шлёт bot/lifecycle.py::notify_tool_call, содержимое и
хранилище — bot/tool_debug.py. Здесь только переключение вида: одно и то же
сообщение переписывается коротким или полным текстом, а кнопка меняет
подпись на противоположную.

Прав не проверяем: сообщение с этой кнопкой приходит только в чаты, которые
сами подписались на дебаг-событие `alfred_tool_call` (event_types в
конфиге) — нажать её может лишь тот, кому его уже показали, и ничего сверх
уже показанного она не открывает.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from sa_home_bot.bot import tool_debug
from sa_home_bot.bot.tool_debug import ToolCalls

router = Router(name="tool_debug")


@router.callback_query(F.data.startswith(f"{tool_debug.CALLBACK_PREFIX}:"))
async def cb_tool_debug(callback: CallbackQuery, tool_calls: ToolCalls) -> None:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or callback.message is None:
        await callback.answer()
        return
    _, mode, token = parts
    call = tool_calls.get(token)
    if call is None:
        # Хранилище живёт в памяти процесса (см. докстринг bot/tool_debug.py):
        # после рестарта бота показывать нечего — говорим прямо, а не молчим.
        await callback.answer(tool_debug.LOST_TEXT, show_alert=True)
        return
    expanded = mode == tool_debug.SHOW
    text = tool_debug.render_full(call) if expanded else tool_debug.render_short(call.name)
    try:
        await callback.message.edit_text(
            text, reply_markup=tool_debug.keyboard(token, expanded=expanded)
        )
    except TelegramBadRequest:
        # «message is not modified» — двойное нажатие; для пользователя это
        # не ошибка, просто ничего не изменилось.
        pass
    await callback.answer()
