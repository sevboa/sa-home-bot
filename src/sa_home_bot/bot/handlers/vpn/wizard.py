from __future__ import annotations

import asyncio
import html

from sa_home_bot.bot import (
    vpn_devices,
    vpn_keyboards,
    vpn_nodes,
    vpn_settings,
    vpn_wizard,
)
from sa_home_bot.bot.handlers.vpn import reports
from sa_home_bot.bot.handlers.vpn.context import (
    Ctx,
    command_or_none,
    fetch_card,
)
from sa_home_bot.bot.handlers.vpn.screens import (
    page_url_by_label,
    redraw_card,
    subscription_page_url,
)
from sa_home_bot.vpn import protocol as vpn_protocol


async def _issue_one(ctx: Ctx, node: str, label: str) -> dict | None:
    return await command_or_none(
        ctx.node_link,
        vpn_protocol.ACTION_ISSUE,
        {
            "chat_id": ctx.chat_id,
            "device_label": label,
            "transport": vpn_protocol.TRANSPORT_REALITY,
        },
        node,
    )


async def create_device(ctx: Ctx, platform_code: str, *, expert: bool = False) -> None:
    platform = vpn_wizard.PLATFORMS.get(platform_code)
    if platform is None:
        await ctx.callback.answer()
        return
    if not await ctx.require_private("Настройка идёт только в личке — напишите мне туда."):
        return
    await ctx.callback.answer("Готовлю подключение…")
    error, servers, _down = await fetch_card(ctx.node_link, ctx.chat_id, ctx.subscription)
    usable = servers if error is None else []
    targets = vpn_nodes.reality_servers(usable)
    devices = vpn_devices.build_devices(servers) if error is None else []
    label = vpn_wizard.next_label(platform, {dev.label for dev in devices})
    results = await asyncio.gather(*(_issue_one(ctx, server["node"], label) for server in targets))
    if not any(result is not None for result in results):
        await ctx.edit(
            vpn_wizard.CREATE_FAILED_TEXT,
            vpn_settings.create_failed_keyboard(platform.code)
            if expert
            else vpn_wizard.create_failed_keyboard(platform.code),
        )
        return
    key = vpn_devices.device_key(label)
    page_url = await page_url_by_label(ctx.node_link, ctx.chat_id, label)
    if expert:
        if page_url is not None:
            await ctx.edit(
                vpn_settings.page_link_text(label, page_url),
                vpn_settings.new_device_link_keyboard(page_url, key),
            )
        else:
            await ctx.edit(vpn_settings.pick_text(label), vpn_settings.pick_keyboard(key))
        return
    await _show_connect_step(ctx, platform, key, label, page_url)


async def _show_connect_step(
    ctx: Ctx, platform: vpn_wizard.Platform, key: str, label: str, page_url: str | None
) -> None:
    if page_url is None:
        await ctx.edit(
            vpn_wizard.UNAVAILABLE_TEXT,
            vpn_wizard.unavailable_keyboard(vpn_wizard.CONNECT, platform.code, key),
        )
        return
    await ctx.edit(
        vpn_wizard.page_link_text(label, page_url),
        vpn_wizard.step_keyboard(vpn_wizard.CONNECT, platform, key, page_url=page_url),
    )


async def show(ctx: Ctx, value: str) -> None:
    callback = ctx.callback
    code, step, rest = vpn_wizard.parse(value)
    if code == vpn_wizard.PICK:
        await callback.answer()
        await ctx.edit(vpn_wizard.PICK_TEXT, vpn_wizard.pick_keyboard())
        return
    if code == vpn_wizard.EXPERT:
        await callback.answer()
        await redraw_card(ctx, expert=True)
        return
    if code == vpn_wizard.DONE:
        await callback.answer()
        await ctx.edit(vpn_wizard.DONE_TEXT, vpn_keyboards.done_keyboard())
        return

    platform = vpn_wizard.PLATFORMS.get(rest[:1])
    key = rest[1:]
    if platform is None:
        await callback.answer()
        await redraw_card(ctx)
        return
    p = platform.code

    if code == vpn_wizard.FAIL:
        await callback.answer()
        await ctx.edit(vpn_wizard.FAIL_TEXT, vpn_wizard.fail_keyboard(step, p, key))
        return
    if code == vpn_wizard.OTHER:
        await callback.answer()
        await ctx.edit(
            vpn_wizard.OTHER_TEXT,
            vpn_wizard.back_to_step_keyboard(step, p, key, help_button=True),
        )
        return

    if code == vpn_wizard.HELP:
        await _help(ctx, platform, step, key)
        return

    if code in (vpn_wizard.INSTALL, vpn_wizard.FILE):
        code = vpn_wizard.CONNECT
    if code not in (vpn_wizard.CONNECT, vpn_wizard.CHECK):
        await callback.answer()
        return
    error, servers, _down = await fetch_card(ctx.node_link, ctx.chat_id, ctx.subscription)
    devices = vpn_devices.build_devices(servers) if error is None else []
    device = vpn_devices.find_device(devices, key) if key else None
    if device is None:
        await callback.answer("Устройство не найдено — начнём заново.", show_alert=True)
        await ctx.edit(vpn_wizard.PICK_TEXT, vpn_wizard.pick_keyboard())
        return
    if code == vpn_wizard.CHECK:
        await _check(ctx, platform, key, device.label)
        return
    await callback.answer()
    page_url = await subscription_page_url(ctx.node_link, ctx.chat_id, device)
    await _show_connect_step(ctx, platform, key, device.label, page_url)


async def _check(ctx: Ctx, platform: vpn_wizard.Platform, key: str, label: str) -> None:
    await ctx.callback.answer("Проверяю подключение…")
    answered = await vpn_nodes.device_online(ctx.node_link, ctx.chat_id, label)
    if not answered:
        await ctx.edit(
            vpn_wizard.CHECK_TEXT, vpn_wizard.step_keyboard(vpn_wizard.CHECK, platform, key)
        )
        return
    found = vpn_wizard.online_connection(answered)
    if found is None:
        await ctx.edit(
            vpn_wizard.NOT_CONNECTED_TEXT, vpn_wizard.not_connected_keyboard(platform.code, key)
        )
        return
    transport, server = found
    method = vpn_devices.TRANSPORT_NAME.get(transport, transport)
    await ctx.edit(vpn_wizard.connected_text(method, server), vpn_keyboards.done_keyboard())


async def _help(ctx: Ctx, platform: vpn_wizard.Platform, step: str, key: str) -> None:
    stuck = vpn_wizard.stuck_text(step, platform)
    reason = html.escape(stuck[:1].upper() + stuck[1:])
    outcome = await reports.submit_report(
        ctx.node_link,
        ctx.notifier,
        ctx.book,
        ctx.store,
        chat_id=ctx.chat_id,
        user=getattr(ctx.callback, "from_user", None),
        subscription=ctx.subscription,
        reason=reason,
        device_key=key,
    )
    if outcome == "throttled":
        await ctx.alert(vpn_wizard.HELP_THROTTLED_TEXT)
        return
    await ctx.callback.answer()
    text = vpn_wizard.HELP_SENT_TEXT if outcome == "sent" else vpn_wizard.HELP_NOBODY_TEXT
    await ctx.edit(
        text, vpn_wizard.back_to_step_keyboard(step, platform.code, key, help_button=False)
    )
