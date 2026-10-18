from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from sa_home_bot.bot import pending_actions as pa
from sa_home_bot.bot.pending_actions import PendingActions

router = Router(name="pending_actions")


@router.callback_query(F.data.startswith(f"{pa.CALLBACK_PREFIX}:"))
async def cb_pending_action(
    callback: CallbackQuery, pending_actions: PendingActions | None = None
) -> None:
    parsed = pa.parse_callback(callback.data)
    if parsed is None or pending_actions is None or callback.from_user is None:
        await callback.answer()
        return
    action_id, button = parsed
    answer = await pending_actions.handle_click(action_id, button, callback.from_user.id)
    await callback.answer(answer)
