from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands, node_view, status_view, swarm_view
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.callback_data import int_or
from sa_home_bot.config import Settings
from sa_home_bot.subscriptions.models import Subscription

router = Router(name="status")


@router.message(Command(commands.STATUS.name))
async def cmd_status(
    message: Message,
    node_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    text, keyboard = await node_view.build_node_card_view(node_link, subscription)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command(commands.STATUS_FULL.name))
async def cmd_status_full(message: Message, node_link: ServiceLink) -> None:
    await message.answer(
        await status_view.build_full_text(node_link, dst=status_view.monitor_dst(None))
    )


def _part(parts: list[str], index: int) -> str | None:
    return parts[index] if len(parts) > index else None


def _node_arg(parts: list[str], index: int) -> str | None:
    return _part(parts, index) or None


@router.callback_query(F.data.startswith(f"{commands.CALLBACK_PREFIX}:"))
async def on_status_action(
    callback: CallbackQuery,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    cmd = commands.command_for_callback(callback.data)
    if cmd is None or callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    code = parts[1]

    if code == commands.NODES_CODE:
        text, keyboard = await swarm_view.build_swarm_view(node_link, subscription, config.wake)
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.NODE_CARD_CODE:
        text, keyboard = await node_view.build_node_card_view(
            node_link, subscription, _node_arg(parts, 2)
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.SERVICE_CARD_CODE:
        text, keyboard = await node_view.build_service_card_view(
            node_link, subscription, _part(parts, 2) or "", _node_arg(parts, 3)
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == "full":
        dst = status_view.monitor_dst(_node_arg(parts, 2))
        await callback.message.answer(await status_view.build_full_text(node_link, dst=dst))
    elif code == "stats":
        dst = status_view.monitor_dst(_node_arg(parts, 2))
        await callback.message.answer(await status_view.build_stats_text(node_link, dst=dst))
    elif code == "downtime":
        text, keyboard = await status_view.build_downtime_page(
            node_link, node_id=_node_arg(parts, 2)
        )
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.DOWNTIME_PAGE_CODE:
        offset = max(0, int_or(_part(parts, 2)))
        text, keyboard = await status_view.build_downtime_page(
            node_link, offset, node_id=_node_arg(parts, 3)
        )
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.answer("Неизвестное действие.")
    await callback.answer()
