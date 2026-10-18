from __future__ import annotations

import asyncio

from sa_home_bot.bot import (
    vpn_callbacks,
    vpn_delivery,
    vpn_devices,
    vpn_nodes,
    vpn_settings,
)
from sa_home_bot.bot.handlers.vpn.context import (
    Ctx,
    command_or_none,
    node_address,
)
from sa_home_bot.bot.handlers.vpn.screens import (
    fresh_device,
    show_screen,
    subscription_page_url,
)
from sa_home_bot.vpn import protocol as vpn_protocol

_cb = vpn_callbacks
_REALITY = vpn_protocol.TRANSPORT_REALITY
_AWG = vpn_protocol.TRANSPORT_AWG
_FILE_UNAVAILABLE = "Сервер не ответил — повторите чуть позже."


def _chat_payload(ctx: Ctx, device: vpn_devices.Device, **extra) -> dict:
    return {"chat_id": ctx.chat_id, "device_label": device.label, **extra}


async def _first_vless_result(ctx: Ctx, device: vpn_devices.Device) -> dict | None:
    conn = next((c for c in device.issued if c.transport == _REALITY), None)
    if conn is None:
        return None
    return await command_or_none(
        ctx.node_link,
        vpn_protocol.ACTION_GET_VLESS,
        _chat_payload(ctx, device),
        conn.node,
    )


async def deliver_vless(ctx: Ctx, key: str) -> None:
    if not await ctx.require_private():
        return
    found = await fresh_device(ctx, key)
    if found is None:
        return
    device, servers = found
    await ctx.callback.answer("Готовлю настройки…")
    if not await vless_send(ctx, device, servers, issue_missing=True):
        await ctx.edit(vpn_settings.UNAVAILABLE_TEXT, vpn_settings.back_keyboard(device.key))


async def _issue_missing_nodes(
    ctx: Ctx, device: vpn_devices.Device, servers: list[dict], *, issue_missing: bool
) -> tuple[list[str], list[str]]:
    added: list[str] = []
    nodes: list[str] = []
    for server in servers:
        node = server.get("node")
        if not node:
            continue
        if device.connection(node, _REALITY) is not None:
            nodes.append(node)
            continue
        if issue_missing and vpn_nodes.has_reality_server([server]):
            issued = await command_or_none(
                ctx.node_link,
                vpn_protocol.ACTION_ISSUE,
                _chat_payload(ctx, device, transport=_REALITY),
                node,
            )
            if issued is not None:
                nodes.append(node)
                added.append(vpn_nodes.server_label(server))
    return nodes, added


async def vless_send(
    ctx: Ctx,
    device: vpn_devices.Device,
    servers: list[dict],
    *,
    issue_missing: bool,
    reissued: bool = False,
) -> bool:
    nodes, added = await _issue_missing_nodes(ctx, device, servers, issue_missing=issue_missing)
    countries = vpn_devices.countries_of(servers)
    results = await asyncio.gather(
        *(
            command_or_none(
                ctx.node_link, vpn_protocol.ACTION_GET_VLESS, _chat_payload(ctx, device), node
            )
            for node in nodes
        )
    )
    got = [(node, res) for node, res in zip(nodes, results, strict=True) if res]
    if not got:
        return False

    wants_file = ctx.config.vpn.wizard_settings_file
    file_needed = wants_file and not vpn_settings.had_vless_handshake(device)
    message_ids: list[int | None] = []
    first = got[0][1]
    config_text = str(first.get("config_text") or "")
    file_sent = False
    if file_needed and config_text:
        file_id = await vpn_delivery.send_config_file(
            ctx.notifier,
            ctx.chat_id,
            transport=_REALITY,
            device_label=device.label,
            config_text=config_text,
            location=str(first.get("location") or ""),
            caption=vpn_settings.VLESS_FILE_CAPTION,
            thread_id=ctx.thread_id,
        )
        message_ids.append(file_id)
        file_sent = file_id is not None

    page_url = await subscription_page_url(ctx.node_link, ctx.chat_id, device)
    multi = len(got) > 1
    tokens: list[str] = []
    qr_buttons: list[tuple[str, str]] = []
    links: list[tuple[str, str]] = []
    for node, res in got:
        country = countries.get(node) or vpn_devices.Country(node, node)
        if res.get("share_url"):
            links.append((country.label, str(res["share_url"])))
        if res.get("qr_png_b64"):
            token = ctx.pending.put(
                str(res.get("config_text") or ""),
                device.label,
                res.get("qr_png_b64"),
                vpn_delivery.REVEAL_QR,
                ctx.ttl_s,
                transport=_REALITY,
                share_url=res.get("share_url"),
                deep_link=res.get("deep_link"),
                location=str(res.get("location") or country.label),
            )
            tokens.append(token)
            qr_buttons.append(
                (
                    vpn_settings.qr_button_text(country, multi),
                    _cb.secret_cb(vpn_protocol.ACTION_ISSUE, token),
                )
            )
    text = vpn_settings.vless_text(
        device.label,
        links,
        has_page=bool(page_url),
        file_sent=file_sent,
        added=added,
        reissued=reissued,
    )
    button_id = await ctx.notifier.send_direct(
        ctx.chat_id,
        text,
        reply_markup=vpn_settings.vless_keyboard(
            page_url=page_url,
            file_again_key=device.key if wants_file and not file_sent else None,
            qr_buttons=qr_buttons,
        ),
        message_thread_id=ctx.thread_id,
    )
    message_ids.append(button_id)
    vpn_delivery.cleanup_later(
        ctx.notifier, ctx.pending, ctx.chat_id, message_ids, tokens, ctx.ttl_s
    )
    return True


async def send_settings_link(ctx: Ctx, key: str) -> None:
    if not await ctx.require_private("Ссылка на настройки выдаётся только в личке."):
        return
    found = await fresh_device(ctx, key)
    if found is None:
        return
    device, _servers = found
    page_url = await subscription_page_url(ctx.node_link, ctx.chat_id, device)
    if page_url is None:
        await ctx.alert("Страница настроек недоступна — попробуйте «Другие способы».")
        return
    await ctx.callback.answer()
    await ctx.notifier.send_direct(
        ctx.chat_id,
        vpn_settings.page_link_text(device.label, page_url),
        reply_markup=vpn_settings.page_link_keyboard(page_url),
        message_thread_id=ctx.thread_id,
    )


async def vless_file_again(ctx: Ctx, key: str) -> None:
    if not await ctx.require_private():
        return
    found = await fresh_device(ctx, key)
    if found is None:
        return
    device, _servers = found
    result = await _first_vless_result(ctx, device)
    config_text = str((result or {}).get("config_text") or "")
    if not config_text:
        await ctx.alert(_FILE_UNAVAILABLE)
        return
    await ctx.callback.answer("Отправляю…")
    file_id = await vpn_delivery.send_config_file(
        ctx.notifier,
        ctx.chat_id,
        transport=_REALITY,
        device_label=device.label,
        config_text=config_text,
        location=str(result.get("location") or ""),
        caption=vpn_settings.VLESS_FILE_CAPTION,
        thread_id=ctx.thread_id,
    )
    vpn_delivery.cleanup_later(ctx.notifier, None, ctx.chat_id, [file_id], [], ctx.ttl_s)


async def awg_country(ctx: Ctx, key: str, node: str | None) -> None:
    found = await fresh_device(ctx, key)
    if found is None:
        return
    device, servers = found
    server = next((s for s in vpn_nodes.awg_servers(servers) if s.get("node") == node), None)
    if server is None or not node:
        await ctx.alert("В этой стране AmneziaVPN сейчас не выдаётся.")
        await show_screen(ctx, _cb.SCREEN_AWG, key)
        return
    if device.connection(node, _AWG) is not None:
        await ctx.callback.answer()
        country = vpn_devices.Country(node, vpn_nodes.server_label(server))
        await ctx.edit(
            vpn_settings.awg_replace_text(device.label, country),
            vpn_settings.awg_replace_keyboard(key, node),
        )
        return
    await awg_deliver(ctx, device, server, vpn_protocol.ACTION_ISSUE)


async def awg_replace(ctx: Ctx, value: str, node_id: str | None) -> None:
    found = await fresh_device(ctx, value[2:])
    if found is None:
        return
    device, servers = found
    server = next((s for s in vpn_nodes.awg_servers(servers) if s.get("node") == node_id), None)
    if server is None:
        await ctx.alert("В этой стране AmneziaVPN сейчас не выдаётся.")
        return
    await awg_deliver(ctx, device, server, vpn_protocol.ACTION_REISSUE)


async def awg_deliver(ctx: Ctx, device: vpn_devices.Device, server: dict, action: str) -> None:
    if not await ctx.require_private():
        return
    await ctx.callback.answer("Выпускаю…")
    node = server["node"]
    result = await ctx.call_or_notice(
        action, _chat_payload(ctx, device, transport=_AWG), node_address(node)
    )
    if result is None:
        return
    await awg_send(ctx, device, vpn_devices.Country(node, vpn_nodes.server_label(server)), result)
    if action == vpn_protocol.ACTION_REISSUE:
        await show_screen(ctx, _cb.SCREEN_AWG, device.key)


async def awg_send(
    ctx: Ctx, device: vpn_devices.Device, country: vpn_devices.Country, result: dict
) -> None:
    config_text = str(result.get("config_text") or "")
    qr_b64 = result.get("qr_png_b64")
    location = str(result.get("location") or "")
    file_id = await vpn_delivery.send_config_file(
        ctx.notifier,
        ctx.chat_id,
        transport=_AWG,
        device_label=device.label,
        config_text=config_text,
        location=location,
        caption=vpn_settings.awg_file_caption(device.label, country),
        thread_id=ctx.thread_id,
    )
    tokens: list[str] = []
    qr_callback = None
    if qr_b64:
        token = ctx.pending.put(
            config_text,
            device.label,
            qr_b64,
            vpn_delivery.REVEAL_QR,
            ctx.ttl_s,
            transport=_AWG,
            location=location,
        )
        tokens.append(token)
        qr_callback = _cb.secret_cb(vpn_protocol.ACTION_ISSUE, token)
    button_id = await ctx.notifier.send_direct(
        ctx.chat_id,
        vpn_settings.awg_steps_text(device.label, country),
        reply_markup=vpn_settings.awg_steps_keyboard(ctx.config.vpn, qr_callback),
        message_thread_id=ctx.thread_id,
    )
    vpn_delivery.cleanup_later(
        ctx.notifier, ctx.pending, ctx.chat_id, [file_id, button_id], tokens, ctx.ttl_s
    )
