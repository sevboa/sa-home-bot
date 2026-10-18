from __future__ import annotations

import contextlib

from sa_home_bot.bot import (
    vpn_admin_view,
    vpn_devices,
    vpn_format,
    vpn_nodes,
    vpn_notify,
)
from sa_home_bot.bot.handlers.vpn.context import (
    GUEST_GONE,
    SERVICE_UNAVAILABLE_NOTICE,
    Ctx,
    fetch_card,
    is_private,
    node_address,
)
from sa_home_bot.bot.menu import refresh_chat_menu
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.pagination import clamp_offset
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ERR_UNKNOWN_ACTION, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

_GUESTS_UNAVAILABLE = "⚠️ Список гостей сейчас недоступен."


def _find_guest(ctx: Ctx, chat_id: int):
    return next((g for g in ctx.book.guests() if g.chat_id == chat_id), None)


def parse_offset(value: str | None) -> int:
    try:
        return max(0, int(value)) if value else 0
    except ValueError:
        return 0


async def handle_admin_screen(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    if ctx.book is None:
        await ctx.alert(_GUESTS_UNAVAILABLE)
        return
    guests = ctx.book.guests()

    rights_chat_id = vpn_admin_view.parse_rights_value(value)
    if rights_chat_id is not None:
        guest = next((g for g in guests if g.chat_id == rights_chat_id), None)
        if guest is None:
            await ctx.alert(GUEST_GONE)
            return
        text, keyboard = vpn_admin_view.build_rights_view(guest)
        await ctx.callback.answer()
        await ctx.edit(text, keyboard)
        return

    chat_id = vpn_admin_view.parse_guest_value(value)
    if chat_id is None:
        offset = parse_offset(value)
        access = {
            guest.chat_id: await vpn_nodes.fanout_usage(ctx.node_link, guest.chat_id)
            for guest in guests
        }
        offset = clamp_offset(offset, vpn_admin_view.GUEST_PAGE_SIZE, len(guests))
        text, keyboard = vpn_admin_view.build_guests_view(guests, access, offset)
        await ctx.callback.answer()
        await ctx.edit(text, keyboard)
        return

    guest = next((g for g in guests if g.chat_id == chat_id), None)
    if guest is None:
        await ctx.alert(GUEST_GONE)
        return
    servers = await vpn_nodes.fanout_usage(ctx.node_link, chat_id)
    if node_id is None:
        text, keyboard = vpn_admin_view.build_guest_view(guest, servers)
    else:
        server = next((s for s in servers if s.get("node") == node_id), None)
        if server is None:
            await ctx.alert("Эта нода сейчас не на связи.")
            return
        text, keyboard = vpn_admin_view.build_location_view(guest, server)
    await ctx.callback.answer()
    await ctx.edit(text, keyboard)


async def handle_set_rights(ctx: Ctx, value: str | None) -> None:
    parsed = vpn_admin_view.parse_set_access_value(value)
    if parsed is None or ctx.book is None or ctx.gate is None:
        await ctx.alert(_GUESTS_UNAVAILABLE)
        return
    chat_id, key = parsed
    toggle = vpn_admin_view.toggle_by_key(key)
    guest = _find_guest(ctx, chat_id)
    if toggle is None or guest is None:
        await ctx.alert(GUEST_GONE)
        return
    updated = ctx.gate.set_guest_rights(chat_id, vpn_admin_view.toggle_rights(guest, toggle))
    if updated is None:
        await ctx.alert(GUEST_GONE)
        return
    was_on = vpn_admin_view.toggle_state(guest, toggle)
    if ctx.bot is not None:
        await refresh_chat_menu(ctx.bot, chat_id, updated)
    await ctx.callback.answer(f"{toggle.label}: {'снято' if was_on else 'выдано'}")
    text, keyboard = vpn_admin_view.build_rights_view(updated)
    await ctx.edit(text, keyboard)


def _set_access_payload(chat_id: int, arg: str) -> dict[str, object] | None:
    payload: dict[str, object] = {"chat_id": chat_id}
    if arg in ("on", "off"):
        payload["allowed"] = arg == "on"
        return payload
    try:
        payload["base_gb"] = int(arg)
    except ValueError:
        return None
    payload["allowed"] = True
    return payload


async def handle_set_access(ctx: Ctx, value: str | None, node_id: str | None) -> None:
    parsed = vpn_admin_view.parse_set_access_value(value)
    if parsed is None or node_id is None or ctx.book is None:
        await ctx.callback.answer()
        return
    chat_id, arg = parsed
    guest = _find_guest(ctx, chat_id)
    if guest is None:
        await ctx.alert(GUEST_GONE)
        return
    payload = _set_access_payload(chat_id, arg)
    if payload is None:
        await ctx.callback.answer()
        return

    try:
        server = await ctx.node_link.command(
            vpn_protocol.ACTION_SET_ACCESS, payload, dst=node_address(node_id)
        )
    except ProtoError as exc:
        if exc.code == ERR_UNKNOWN_ACTION:
            await ctx.alert(f"Нода «{node_id}» ещё не умеет выдавать доступ — обновите её.")
        else:
            await ctx.alert(f"⚠️ {exc.message}")
        return
    except ServiceUnavailableError:
        await ctx.alert(SERVICE_UNAVAILABLE_NOTICE)
        return

    await ctx.callback.answer(_access_toast(server))
    await notify_guest_access(
        ctx.notifier, chat_id, server, opened=bool(server.get("allowed")), node_link=ctx.node_link
    )
    text, keyboard = vpn_admin_view.build_location_view(guest, server)
    await ctx.edit(text, keyboard)


def _access_toast(server: dict) -> str:
    where = vpn_admin_view.server_name(server)
    if not server.get("allowed"):
        return f"{where}: доступ закрыт"
    base_gb = vpn_format.bytes_to_gb(server.get("base_limit_bytes", 0))
    return f"{where}: открыт, {base_gb:.0f} ГБ"


async def notify_guest_access(
    notifier: Notifier,
    chat_id: int,
    server: dict,
    *,
    opened: bool,
    node_link: ServiceLink | None = None,
) -> None:
    if not is_private(chat_id):
        return
    country = vpn_devices.Country(
        str(server.get("node") or ""), str(server.get("label") or server.get("node") or "")
    )
    if opened:
        has_devices: bool | None = None
        if node_link is not None:
            with contextlib.suppress(Exception):
                error, servers, _down = await fetch_card(node_link, chat_id)
                has_devices = bool(vpn_devices.build_devices(servers)) if error is None else None
        base_gb = vpn_format.bytes_to_gb(server.get("base_limit_bytes", 0))
        text = vpn_notify.access_opened_text(country, base_gb, has_devices=has_devices)
        markup = vpn_notify.access_keyboard(has_devices=has_devices)
    else:
        text, markup = vpn_notify.access_closed_text(country), None
    with contextlib.suppress(Exception):
        await notifier.send_direct(chat_id, text, reply_markup=markup)
