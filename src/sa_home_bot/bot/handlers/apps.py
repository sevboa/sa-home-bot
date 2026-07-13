"""Динамические команды-скилы из describe службы apps (/qbittorrent, …).

Имена команд не захардкожены: сообщение с командой сверяется с актуальным
describe (кэш линка). Роутер включается ПОСЛЕДНИМ — все статические команды
уже разобраны раньше, чужие/неизвестные команды молча игнорируются (как и
до этого). Права: `<id>@apps` (authorization-middleware реестровых команд
про них не знает, поэтому проверка здесь).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from sa_home_bot.bot import apps_view
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
    action = next(
        (a for a in await apps_link.actions() if a.id == command), None
    )
    if action is None:
        return  # не скилл-приложение — игнор, как любую неизвестную команду
    if subscription is None or not subscription.allows_action(
        command, apps_view.APPS_SERVICE
    ):
        log.info("Отказ в скилле /%s для chat_id=%s", command, message.chat.id)
        await message.answer(DENIED_TEXT)
        return
    text, keyboard = await apps_view.run_app_skill(apps_link, subscription, command)
    await message.answer(text, reply_markup=keyboard, disable_web_page_preview=True)
