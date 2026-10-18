from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from sa_home_bot.bot import apps_view, commands
from sa_home_bot.bot.middlewares import DENIED_TEXT, extract_command
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="apps")


@router.message(F.text.startswith("/"))
async def cmd_app_skill(
    message: Message,
    apps_link: ServiceLink,
    subscription: Subscription | None = None,
) -> None:
    command = extract_command(message.text)
    if command is None:
        return
    if command in apps_view.HIDDEN_MENU_APP_IDS:
        return
    action = next(
        (a for a in await apps_link.actions() if a.id == command), None
    )
    if action is None:
        return
    if not commands.can_run_action(subscription, command, apps_view.APPS_SERVICE):
        log.info("Отказ в скилле /%s для chat_id=%s", command, message.chat.id)
        await message.answer(DENIED_TEXT)
        return
    text, keyboard = await apps_view.run_app_skill(apps_link, subscription, command)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
