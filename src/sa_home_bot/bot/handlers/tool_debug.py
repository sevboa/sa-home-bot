from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from sa_home_bot.bot import tool_debug
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.bot.ui.callback_data import unpack
from sa_home_bot.bot.ui.redraw import redraw

router = Router(name="tool_debug")


@router.callback_query(F.data.startswith(f"{tool_debug.CALLBACK_PREFIX}:"))
async def cb_tool_debug(callback: CallbackQuery, tool_calls: ToolCalls) -> None:
    parsed = unpack(callback.data, tool_debug.CALLBACK_PREFIX, 2)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    mode, token = parsed
    call = tool_calls.get(token)
    if call is None:
        await callback.answer(tool_debug.LOST_TEXT, show_alert=True)
        return
    expanded = mode == tool_debug.SHOW
    text = tool_debug.render_full(call) if expanded else tool_debug.render_short(call.name)
    await redraw(callback, text, tool_debug.keyboard(token, expanded=expanded))
    await callback.answer()
