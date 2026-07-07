"""/node — состояние ноды и служб; обработка динамических действий «act:…».

Права на callback уже проверены CallbackAuthorizationMiddleware
(`действие@служба`). Здесь только маршрутизация к нужному линку и рендер.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import actions, commands, node_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="node")


@router.message(Command(commands.NODE.name))
async def cmd_node(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    text, keyboard = await node_view.build_node_view(node_link, subscription)
    await message.answer(text, reply_markup=keyboard)


async def _run_node_action(
    node_link: ServiceLink, action_id: str, value: str | None
) -> str | None:
    """Выполнить действие ноды; вернуть текст ошибки или None при успехе."""
    action = await actions.find_action(node_link, action_id)
    if action is None:
        return "Действие недоступно."
    try:
        await node_link.command(action.id, actions.build_args(action, value))
    except ServiceUnavailableError:
        return node_view.NODE_DOWN_TEXT
    except ProtoError as exc:
        return f"⚠️ Ошибка: {exc.message}"
    return None


@router.callback_query(F.data.startswith(f"{commands.ACTION_CALLBACK_PREFIX}:"))
async def on_dynamic_action(
    callback: CallbackQuery,
    store: Store,
    link: ServiceLink,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    service, action_id, value = parsed

    if service == node_view.NODE_SERVICE:
        error = await _run_node_action(node_link, action_id, value)
        if error is not None:
            await callback.message.answer(error)
        else:
            # Перерисовать карточку ноды свежим состоянием.
            text, keyboard = await node_view.build_node_view(node_link, subscription)
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer("Готово" if error is None else None)
        return

    if service == "monitor":
        text = await actions.run_action(store, link, service, action_id, value)
        await callback.message.answer(text)
        await callback.answer()
        return

    log.warning("Callback для неизвестной службы: %s", callback.data)
    await callback.answer("Неизвестная служба", show_alert=True)
