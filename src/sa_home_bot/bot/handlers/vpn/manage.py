from __future__ import annotations

from sa_home_bot.bot import (
    vpn_callbacks,
    vpn_devices,
    vpn_rights,
    vpn_settings,
)
from sa_home_bot.bot.handlers.vpn.context import (
    DEVICE_NOT_FOUND,
    Ctx,
    fetch_card,
    node_address,
)
from sa_home_bot.bot.handlers.vpn.delivery import awg_send, vless_send
from sa_home_bot.bot.handlers.vpn.screens import fresh_device, show_screen
from sa_home_bot.bot.service_link import ServiceUnavailableError
from sa_home_bot.bot.vpn_delivery import send_secret
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

_cb = vpn_callbacks
_TRANSPORT_BY_KIND = {
    _cb.KIND_REISSUE_REALITY: vpn_protocol.TRANSPORT_REALITY,
    _cb.KIND_REISSUE_AWG: vpn_protocol.TRANSPORT_AWG,
}


async def _find_device(ctx: Ctx, key: str) -> tuple[vpn_devices.Device | None, list[dict]]:
    error, servers, _down = await fetch_card(ctx.node_link, ctx.chat_id, ctx.subscription)
    if error is not None:
        return None, servers
    return vpn_devices.find_device(vpn_devices.build_devices(servers), key), servers


async def _device_or_back_to_list(
    ctx: Ctx, key: str
) -> tuple[vpn_devices.Device, list[dict]] | None:
    device, servers = await _find_device(ctx, key)
    if device is None:
        await ctx.alert(DEVICE_NOT_FOUND)
        await show_screen(ctx, _cb.SCREEN_LIST)
        return None
    return device, servers


async def expert_reissue(ctx: Ctx, value: str, node_id: str | None) -> None:
    if not await ctx.require_private():
        return
    kind, key = value[1:2], value[2:]
    device, _servers = await _find_device(ctx, key)
    if device is None or not node_id:
        await ctx.alert(DEVICE_NOT_FOUND)
        await show_screen(ctx, _cb.SCREEN_LIST)
        return
    if kind == _cb.KIND_FIX:
        transports = [c.transport for c in device.broken_in(node_id)]
    else:
        transport = _TRANSPORT_BY_KIND.get(kind)
        found = device.connection(node_id, transport) if transport else None
        transports = [found.transport] if found is not None else []
    if not transports:
        await ctx.alert("Тут нечего перевыпускать.")
        await show_screen(ctx, _cb.SCREEN_DEVICE, key)
        return
    await ctx.callback.answer("Перевыпускаю…")
    dst = node_address(node_id)
    for transport in transports:
        try:
            result = await ctx.node_link.command(
                vpn_protocol.ACTION_REISSUE,
                {"chat_id": ctx.chat_id, "device_label": device.label, "transport": transport},
                dst=dst,
            )
        except ProtoError as exc:
            await ctx.callback.message.answer(f"⚠️ {exc.message}")
            continue
        except ServiceUnavailableError:
            await ctx.callback.message.answer("⚠️ Служба VPN недоступна.")
            break
        await send_secret(
            ctx.notifier,
            ctx.pending,
            ctx.chat_id,
            vpn_protocol.ACTION_REISSUE,
            result,
            ctx.ttl_s,
            message_thread_id=ctx.thread_id,
        )
    await show_screen(ctx, _cb.SCREEN_DEVICE, key)


async def show_reissue_select(ctx: Ctx, rest: str, *, answered: bool, note: str = "") -> None:
    key, sig, mask = vpn_settings.parse_selection(rest)
    found = await fresh_device(ctx, key)
    if found is None:
        return
    device, servers = found
    conns = vpn_settings.reissue_connections(device)
    if not answered:
        await ctx.callback.answer()
    if sig is not None and (
        sig != vpn_settings.list_signature(conns) or mask & ~vpn_settings.all_mask(conns)
    ):
        mask, note = 0, vpn_settings.LIST_CHANGED_TEXT
    elif sig is None:
        mask = 0
    await ctx.edit(
        vpn_settings.reissue_select_text(device.label, note),
        vpn_settings.reissue_select_keyboard(
            device,
            conns,
            vpn_devices.countries_of(servers),
            mask,
            can_run=vpn_rights.allows(ctx.subscription, vpn_protocol.ACTION_REISSUE),
        ),
    )


async def show_restore_select(ctx: Ctx, rest: str) -> None:
    key, node_hash, codes = vpn_settings.parse_restore(rest)
    found = await fresh_device(ctx, key)
    if found is None:
        return
    device, _servers = found
    conns = vpn_settings.reissue_connections(device)
    mask = vpn_settings.restore_mask(conns, node_hash, codes)
    await show_reissue_select(
        ctx, f"{key}-{vpn_settings.list_signature(conns)}-{mask:x}", answered=False
    )


async def _reissue_selected(
    ctx: Ctx, device: vpn_devices.Device, chosen: list[vpn_devices.Connection], countries: dict
) -> tuple[list[str], list[tuple[str, str]], list[tuple[vpn_devices.Connection, dict]]]:
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    results: list[tuple[vpn_devices.Connection, dict]] = []
    for conn in chosen:
        name = vpn_settings.reissue_conn_text(conn, countries)
        try:
            result = await ctx.node_link.command(
                vpn_protocol.ACTION_REISSUE,
                {
                    "chat_id": ctx.chat_id,
                    "device_label": device.label,
                    "transport": conn.transport,
                },
                dst=node_address(conn.node),
            )
        except ProtoError as exc:
            failed.append((name, exc.message))
            continue
        except ServiceUnavailableError:
            failed.append((name, "сервер не ответил"))
            continue
        done.append(name)
        results.append((conn, result))
    return done, failed, results


async def expert_reissue_multi(ctx: Ctx, rest: str) -> None:
    if not await ctx.require_private():
        return
    key, sig, mask = vpn_settings.parse_selection(rest)
    device, servers = await _find_device(ctx, key)
    if device is None:
        await ctx.alert(DEVICE_NOT_FOUND)
        await show_screen(ctx, _cb.SCREEN_LIST)
        return
    conns = vpn_settings.reissue_connections(device)
    if sig != vpn_settings.list_signature(conns) or mask & ~vpn_settings.all_mask(conns):
        await ctx.alert(vpn_settings.LIST_CHANGED_TEXT)
        await show_reissue_select(ctx, key, answered=True, note=vpn_settings.LIST_CHANGED_TEXT)
        return
    chosen = vpn_settings.selected(conns, mask)
    if not chosen:
        await ctx.alert(vpn_settings.NOTHING_PICKED_TEXT)
        return
    await ctx.callback.answer("Перевыпускаю…")
    countries = vpn_devices.countries_of(servers)
    done, failed, results = await _reissue_selected(ctx, device, chosen, countries)
    for conn, result in results:
        if conn.transport == vpn_protocol.TRANSPORT_AWG:
            await awg_send(
                ctx,
                device,
                countries.get(conn.node) or vpn_devices.Country(conn.node, conn.node),
                result,
            )
    if any(conn.transport == vpn_protocol.TRANSPORT_REALITY for conn, _ in results):
        fresh, fresh_servers = await _find_device(ctx, key)
        sent = fresh is not None and await vless_send(
            ctx, fresh, fresh_servers, issue_missing=False, reissued=True
        )
        if not sent:
            failed.append(
                (
                    "VLESS · Hiddify — настройки",
                    "ключи перевыпущены, но настройки не получены: нажмите «📥 Получить настройки»",
                )
            )
    await ctx.edit(
        vpn_settings.reissue_result_text(device.label, done, failed),
        vpn_settings.reissue_result_keyboard(key),
    )


async def expert_delete(ctx: Ctx, value: str) -> None:
    key = value[2:]
    found = await _device_or_back_to_list(ctx, key)
    if found is None:
        return
    device, _servers = found
    failed = 0
    for conn in device.issued:
        try:
            await ctx.node_link.command(
                vpn_protocol.ACTION_REVOKE,
                {
                    "chat_id": ctx.chat_id,
                    "device_label": device.label,
                    "transport": conn.transport,
                },
                dst=node_address(conn.node),
            )
        except (ProtoError, ServiceUnavailableError):
            failed += 1
    if failed:
        await ctx.alert(f"Удалено не всё: {failed} подключ. не отвечают. Повторите позже.")
        await show_screen(ctx, _cb.SCREEN_DEVICE, key)
        return
    await ctx.callback.answer(f"Удалено: {device.label}")
    await show_screen(ctx, _cb.SCREEN_LIST)
