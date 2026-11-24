from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardMarkup

from sa_home_bot.bot import (
    vpn_callbacks,
    vpn_devices,
    vpn_legacy_card,
    vpn_nodes,
    vpn_proxy_screen,
    vpn_rights,
    vpn_screens,
    vpn_settings,
    vpn_wizard,
)
from sa_home_bot.bot.handlers.vpn.context import (
    DEVICE_NOT_FOUND,
    NO_VPN_ACCESS,
    Ctx,
    fetch_card,
    is_private,
    self_serve_nodes,
)
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.config import Settings
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

_cb = vpn_callbacks


def wizard_available(servers: list[dict], subscription: Subscription | None) -> bool:
    return vpn_rights.allows(subscription, vpn_protocol.ACTION_ISSUE) and (
        vpn_nodes.has_reality_server(servers)
    )


def render_main(
    servers: list[dict],
    down: list[dict],
    subscription: Subscription | None,
    config: Settings,
    *,
    wizard: bool = False,
    expert: bool = False,
) -> tuple[str, InlineKeyboardMarkup]:
    self_serve = self_serve_nodes(servers, config)
    has_devices = bool(vpn_devices.build_devices(servers))
    if has_devices or (expert and wizard_available(servers, subscription)):
        return (
            vpn_devices.home_text(servers, unavailable=down),
            vpn_screens.home_keyboard(
                servers,
                subscription=subscription,
                self_serve_nodes=self_serve,
                has_devices=has_devices,
                single_device=single_reality_device(servers),
            ),
        )
    if wizard and wizard_available(servers, subscription):
        return vpn_wizard.INTRO_TEXT, vpn_wizard.intro_keyboard()
    return (
        vpn_legacy_card.usage_text(servers, unavailable=down),
        vpn_legacy_card.card_keyboard(
            servers, subscription=subscription, self_serve_nodes=self_serve
        ),
    )


def single_reality_device(servers: list[dict]) -> vpn_devices.Device | None:
    devices = vpn_devices.build_devices(servers)
    if len(devices) != 1:
        return None
    if not any(c.transport == vpn_protocol.TRANSPORT_REALITY for c in devices[0].issued):
        return None
    return devices[0]


def proxy_only_card(error: str | None, subscription: Subscription | None) -> bool:
    return error is NO_VPN_ACCESS and vpn_rights.allows(
        subscription, vpn_protocol.ACTION_PROXY_LINK
    )


async def redraw_card(ctx: Ctx, *, expert: bool = False) -> None:
    error, servers, down = await fetch_card(ctx.node_link, ctx.chat_id, ctx.subscription)
    if error is not None:
        if proxy_only_card(error, ctx.subscription):
            await ctx.edit(vpn_proxy_screen.ONLY_PROXY_CARD, vpn_proxy_screen.only_proxy_keyboard())
        return
    text, keyboard = render_main(
        servers,
        down,
        ctx.subscription,
        ctx.config,
        wizard=not expert and is_private(ctx.chat_id),
        expert=expert,
    )
    await ctx.edit(text, keyboard)


async def subscription_answer(
    node_link: ServiceLink, chat_id: int, device: vpn_devices.Device
) -> dict | None:
    if not any(c.transport == vpn_protocol.TRANSPORT_REALITY for c in device.issued):
        return None
    return await subscription_answer_by_label(node_link, chat_id, device.label)


async def subscription_page_url(
    node_link: ServiceLink, chat_id: int, device: vpn_devices.Device
) -> str | None:
    answer = await subscription_answer(node_link, chat_id, device)
    return str(answer["page_url"]) if answer else None


def subscription_link(answer: dict | None) -> str | None:
    if not answer:
        return None
    return str(answer.get("singbox_url") or answer.get("sub_url") or "") or None


def page_url_rank(url: str) -> int:
    parts = urlsplit(url)
    if parts.scheme != "https":
        return 2
    try:
        ipaddress.ip_address(parts.hostname or "")
    except ValueError:
        return 0
    return 1


async def subscription_answer_by_label(
    node_link: ServiceLink, chat_id: int, label: str
) -> dict | None:
    answers = await vpn_nodes.fanout(
        node_link,
        vpn_protocol.ACTION_GET_SUBSCRIPTION,
        {"chat_id": chat_id, "device_label": label},
    )
    found = [a for a in answers if a.get("page_url")]
    return min(found, key=lambda a: page_url_rank(str(a["page_url"]))) if found else None


async def page_url_by_label(node_link: ServiceLink, chat_id: int, label: str) -> str | None:
    answer = await subscription_answer_by_label(node_link, chat_id, label)
    return str(answer["page_url"]) if answer else None


async def show_screen(ctx: Ctx, screen: str, key: str = "", *, answered: bool = True) -> None:
    callback = ctx.callback
    error, servers, _down = await fetch_card(ctx.node_link, ctx.chat_id, ctx.subscription)
    if error is not None:
        if not answered:
            await callback.answer()
        await redraw_card(ctx)
        return
    devices = vpn_devices.build_devices(servers)
    device = vpn_devices.find_device(devices, key) if key else None
    if screen not in (_cb.SCREEN_LIST, _cb.SCREEN_NEW) and device is None:
        if not answered:
            await callback.answer("Устройство не найдено — обновил список.", show_alert=True)
            answered = True
        screen = _cb.SCREEN_LIST
    if not answered:
        await callback.answer()
    if screen == _cb.SCREEN_DEVICE:
        page_url = await subscription_page_url(ctx.node_link, ctx.chat_id, device)
        text = vpn_devices.card_text(device, servers, has_page=page_url is not None)
        keyboard = vpn_screens.device_card_keyboard(
            device, servers, subscription=ctx.subscription, page_url=page_url
        )
    elif screen == _cb.SCREEN_DELETE:
        text, keyboard = vpn_devices.delete_text(device), vpn_screens.delete_keyboard(device)
    elif screen == _cb.SCREEN_PICK:
        text = vpn_settings.pick_text(device.label)
        page_url = await subscription_page_url(ctx.node_link, ctx.chat_id, device)
        keyboard = vpn_settings.pick_keyboard(device.key, page_url=page_url)
    elif screen == _cb.SCREEN_AWG:
        awg_servers = vpn_nodes.awg_servers(servers)
        text = vpn_settings.awg_pick_text(device.label) if awg_servers else vpn_settings.NO_AWG_TEXT
        keyboard = vpn_settings.awg_pick_keyboard(device.key, awg_servers)
    elif screen == _cb.SCREEN_NEW:
        text, keyboard = vpn_wizard.PICK_TEXT, vpn_settings.new_device_keyboard()
    else:
        text = vpn_devices.list_text(devices)
        keyboard = vpn_screens.list_keyboard(devices, subscription=ctx.subscription)
    await ctx.edit(text, keyboard)


async def fresh_device(ctx: Ctx, key: str) -> tuple[vpn_devices.Device, list[dict]] | None:
    error, servers, _down = await fetch_card(ctx.node_link, ctx.chat_id, ctx.subscription)
    device = (
        vpn_devices.find_device(vpn_devices.build_devices(servers), key) if error is None else None
    )
    if device is None:
        await ctx.alert(DEVICE_NOT_FOUND)
        await show_screen(ctx, _cb.SCREEN_LIST)
        return None
    return device, servers
