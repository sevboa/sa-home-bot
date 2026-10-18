from __future__ import annotations

from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, Message

from sa_home_bot.bot import (
    commands,
    vpn_callbacks,
    vpn_legacy_card,
    vpn_nodes,
    vpn_proxy_screen,
)
from sa_home_bot.bot.handlers.vpn.context import VPN_UNAVAILABLE, fetch_card, is_private
from sa_home_bot.bot.handlers.vpn.router import router
from sa_home_bot.bot.handlers.vpn.screens import proxy_only_card, render_main
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.config import Settings
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol


async def usage_text(node_link: ServiceLink, chat_id: int) -> str:
    servers = await vpn_nodes.fanout_usage(node_link, chat_id)
    if not servers:
        return VPN_UNAVAILABLE
    return vpn_legacy_card.usage_text(servers, show_access=True)


@router.message(Command(commands.VPN.name))
async def cmd_vpn(
    message: Message,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    error, servers, down = await fetch_card(node_link, message.chat.id, subscription)
    if error is not None:
        if proxy_only_card(error, subscription):
            await message.answer(
                vpn_proxy_screen.ONLY_PROXY_CARD,
                reply_markup=vpn_proxy_screen.only_proxy_keyboard(),
            )
            return
        await message.answer(error)
        return
    text, keyboard = render_main(
        servers, down, subscription, config, wizard=is_private(message.chat.id)
    )
    await message.answer(text, reply_markup=keyboard)


def resolve_request_callback(request_id: int) -> InlineKeyboardMarkup:
    return markup(
        [
            button(
                "✅ Одобрить",
                vpn_callbacks.vpn_callback(
                    vpn_protocol.ACTION_RESOLVE_REQUEST, f"{request_id}_approve"
                ),
            ),
            button(
                "🚫 Отклонить",
                vpn_callbacks.vpn_callback(
                    vpn_protocol.ACTION_RESOLVE_REQUEST, f"{request_id}_deny"
                ),
            ),
        ]
    )
