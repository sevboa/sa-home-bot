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

from sa_home_bot.bot import commands, node_view, status_view, swarm_view
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="status")


@router.message(Command(commands.STATUS.name))
async def cmd_status(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    # /status — ярлык карточки локальной ноды (мониторинг + службы).
    text, keyboard = await node_view.build_node_card_view(node_link, subscription)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command(commands.STATUS_FULL.name))
async def cmd_status_full(message: Message, node_link: ServiceLink) -> None:
    await message.answer(
        await status_view.build_full_text(node_link, dst=status_view.monitor_dst(None))
    )


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
        # Старые кнопки «Список нод» из чатов — теперь сводка роя.
        text, keyboard = await swarm_view.build_swarm_view(
            node_link, subscription, config.wake
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.NODE_CARD_CODE:
        node_id = parts[2] if len(parts) > 2 and parts[2] else None
        text, keyboard = await node_view.build_node_card_view(
            node_link, subscription, node_id
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.SERVICE_CARD_CODE:
        name = parts[2] if len(parts) > 2 else ""
        node_id = parts[3] if len(parts) > 3 and parts[3] else None
        text, keyboard = await node_view.build_service_card_view(
            node_link, subscription, name, node_id
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == "full":
        node_id = parts[2] if len(parts) > 2 and parts[2] else None
        await callback.message.answer(
            await status_view.build_full_text(
                node_link, dst=status_view.monitor_dst(node_id)
            )
        )
    elif code == "stats":
        node_id = parts[2] if len(parts) > 2 and parts[2] else None
        await callback.message.answer(
            await status_view.build_stats_text(
                node_link, dst=status_view.monitor_dst(node_id)
            )
        )
    elif code == "downtime":
        # Кнопка на карточке ноды — новая страница отдельным сообщением.
        node_id = parts[2] if len(parts) > 2 and parts[2] else None
        text, keyboard = await status_view.build_downtime_page(node_link, node_id=node_id)
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.DOWNTIME_PAGE_CODE:
        # Кнопка «Следующие/Предыдущие 10» — редактируем то же сообщение.
        node_id = parts[3] if len(parts) > 3 and parts[3] else None
        text, keyboard = await status_view.build_downtime_page(
            node_link, _parse_offset(parts), node_id=node_id
        )
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.answer("Неизвестное действие.")
    await callback.answer()
