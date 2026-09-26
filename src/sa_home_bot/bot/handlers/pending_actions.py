"""Кнопки форм подтверждения (Этап 45, bot/pending_actions.py).

Один обработчик на все формы: callback_data «pa:<id>:<кнопка>» несёт
только id записи и вердикт, всё остальное — в БД бота. Права проверяет
PendingActions.handle_click по from_user.id (инициатор решает черновик,
адресат — входящее предложение), а не по тому, в каком чате кнопка: гость
не может нажать за другого, даже если форму ему переслали.
"""

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
