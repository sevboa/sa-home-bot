"""/nodes — список нод роя; обработка динамических действий «act:…».

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

from sa_home_bot.bot import actions, apps_view, commands, node_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="node")


@router.message(Command(commands.NODES.name))
async def cmd_nodes(
    message: Message,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    text, keyboard = await node_view.build_nodes_list_view(
        node_link, subscription, config.wake
    )
    await message.answer(text, reply_markup=keyboard)


async def _run_node_action(
    node_link: ServiceLink, action_id: str, value: str | None, node_id: str | None
) -> str | None:
    """Выполнить действие ноды (свою или пира); текст ошибки или None при успехе."""
    dst = Address(node=node_id, service=node_view.NODE_SERVICE) if node_id else None
    action = await actions.find_action(node_link, action_id, dst=dst)
    if action is None:
        return "Действие недоступно."
    try:
        await node_link.command(action.id, actions.build_args(action, value), dst=dst)
    except ServiceUnavailableError:
        return (
            f"⚠️ Нода «{node_id}» недоступна (нет связи или она спит)."
            if node_id
            else node_view.NODE_DOWN_TEXT
        )
    except ProtoError as exc:
        return f"⚠️ Ошибка: {exc.message}"
    return None


@router.callback_query(F.data.startswith(f"{commands.ACTION_CALLBACK_PREFIX}:"))
async def on_dynamic_action(
    callback: CallbackQuery,
    store: Store,
    link: ServiceLink,
    node_link: ServiceLink,
    apps_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    service, action_id, value, node_id = parsed

    if service == node_view.NODE_SERVICE:
        error = await _run_node_action(node_link, action_id, value, node_id)
        if error is not None:
            await callback.message.answer(error)
        elif value is not None:
            # Действие над службой (своей или пира) — перерисовать её карточку.
            text, keyboard = await node_view.build_service_card_view(
                node_link, subscription, value, node_id
            )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=keyboard)
        elif node_id is not None:
            # Питание пира — перерисовать его карточку.
            text, keyboard = await node_view.build_remote_node_card_view(
                node_link, subscription, node_id
            )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=keyboard)
        else:
            text, keyboard = await node_view.build_nodes_list_view(
                node_link, subscription, config.wake
            )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer("Готово" if error is None else None)
        return

    if service == apps_view.APPS_SERVICE:
        # Кнопки act:apps из старых сообщений — тот же скилл, что команда.
        text = await apps_view.run_app_skill(apps_link, action_id, value)
        await callback.message.answer(text, disable_web_page_preview=True)
        await callback.answer()
        return

    if service == "monitor":
        text = await actions.run_action(store, link, service, action_id, value)
        await callback.message.answer(text)
        await callback.answer()
        return

    log.warning("Callback для неизвестной службы: %s", callback.data)
    await callback.answer("Неизвестная служба", show_alert=True)
