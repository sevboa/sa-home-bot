"""/status (краткая сводка + кнопки), /status_full и обработка кнопок-действий."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import commands, status_view
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.worker.queue import DedupQueue

log = logging.getLogger(__name__)

router = Router(name="status")


@router.message(Command(commands.STATUS.name))
async def cmd_status(
    message: Message,
    store: Store,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    text = await status_view.build_summary_text(store, config)
    keyboard = status_view.build_status_keyboard(subscription)
    await message.answer(text, reply_markup=keyboard)


@router.message(Command(commands.STATUS_FULL.name))
async def cmd_status_full(message: Message, store: Store) -> None:
    await message.answer(await status_view.build_full_text(store))


async def _dispatch_action(code: str, store: Store, queue: DedupQueue) -> str:
    if code == "full":
        return await status_view.build_full_text(store)
    if code == "stats":
        return await status_view.build_stats_text(store)
    if code == "scan":
        return await status_view.build_scan_text(store, queue)
    return "Неизвестное действие."


def _parse_offset(parts: list[str]) -> int:
    if len(parts) <= 2:
        return 0
    try:
        return max(0, int(parts[2]))
    except ValueError:
        return 0


@router.callback_query(F.data.startswith(f"{commands.CALLBACK_PREFIX}:"))
async def on_status_action(
    callback: CallbackQuery, store: Store, queue: DedupQueue
) -> None:
    # Права уже проверены CallbackAuthorizationMiddleware.
    cmd = commands.command_for_callback(callback.data)
    if cmd is None or callback.message is None:
        await callback.answer()
        return
    parts = callback.data.split(":")
    code = parts[1]

    if code == "downtime":
        # Кнопка под /status — новая страница отдельным сообщением.
        text, keyboard = await status_view.build_downtime_page()
        await callback.message.answer(text, reply_markup=keyboard)
    elif code == commands.DOWNTIME_PAGE_CODE:
        # Кнопка «Следующие/Предыдущие 10» — редактируем то же сообщение.
        text, keyboard = await status_view.build_downtime_page(_parse_offset(parts))
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        text = await _dispatch_action(code, store, queue)
        await callback.message.answer(text)
    await callback.answer()
