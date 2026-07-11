"""/status (карточка ноды) , /status_full и кнопки-представления «st:…».

Иерархия раздела нод: «st:nodes» — список нод, «st:nodecard» — карточка ноды
(/status), «st:svc:<имя>» — карточка службы. Права проверены
CallbackAuthorizationMiddleware до входа сюда.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands, node_view, status_view
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="status")


@router.message(Command(commands.STATUS.name))
async def cmd_status(
    message: Message,
    link: ServiceLink,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    # /status — ярлык карточки локальной ноды (мониторинг + службы).
    text, keyboard = await node_view.build_node_card_view(link, node_link, subscription)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command(commands.STATUS_FULL.name))
async def cmd_status_full(message: Message, link: ServiceLink) -> None:
    await message.answer(await status_view.build_full_text(link))


def _parse_offset(parts: list[str]) -> int:
    if len(parts) <= 2:
        return 0
    try:
        return max(0, int(parts[2]))
    except ValueError:
        return 0


@router.callback_query(F.data.startswith(f"{commands.CALLBACK_PREFIX}:"))
async def on_status_action(
    callback: CallbackQuery,
    store: Store,
    link: ServiceLink,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    # Права уже проверены CallbackAuthorizationMiddleware.
    cmd = commands.command_for_callback(callback.data)
    if cmd is None or callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    code = parts[1]

    if code == commands.NODES_CODE:
        text, keyboard = await node_view.build_nodes_list_view(
            node_link, subscription, config.wake
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.NODE_CARD_CODE:
        node_id = parts[2] if len(parts) > 2 and parts[2] else None
        if node_id:
            text, keyboard = await node_view.build_remote_node_card_view(
                node_link, subscription, node_id
            )
        else:
            text, keyboard = await node_view.build_node_card_view(
                link, node_link, subscription
            )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.SERVICE_CARD_CODE:
        name = parts[2] if len(parts) > 2 else ""
        text, keyboard = await node_view.build_service_card_view(
            node_link, subscription, name
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == "full":
        await callback.message.answer(await status_view.build_full_text(link))
    elif code == "stats":
        await callback.message.answer(await status_view.build_stats_text(link))
    elif code == "downtime":
        # Кнопка на карточке ноды — новая страница отдельным сообщением.
        text, keyboard = await status_view.build_downtime_page()
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.DOWNTIME_PAGE_CODE:
        # Кнопка «Следующие/Предыдущие 10» — редактируем то же сообщение.
        text, keyboard = await status_view.build_downtime_page(_parse_offset(parts))
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.answer("Неизвестное действие.")
    await callback.answer()
