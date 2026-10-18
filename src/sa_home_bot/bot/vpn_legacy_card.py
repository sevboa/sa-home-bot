from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import (
    vpn_callbacks,
    vpn_check_view,
    vpn_devices,
    vpn_format,
    vpn_keyboards,
    vpn_rights,
    vpn_screens,
)
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.bot.vpn_nodes import is_allowed, server_label
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

_cb = vpn_callbacks


def _device_line(device: dict) -> str:
    if device.get("broken"):
        return (
            f"• 🔧 «{html.escape(device['device_label'])}» не отвечает — "
            "сервер его не помнит, перевыпустите"
        )
    handshake = device.get("last_handshake_at")
    seen = (
        f", было на связи {handshake[:16].replace('T', ' ')}"
        if handshake
        else ", ещё не подключалось"
    )
    transport = device.get("transport")
    tag = (
        f" · {vpn_devices.TECHNICAL_TRANSPORT_NAME.get(transport, transport)}" if transport else ""
    )
    return f"• {html.escape(device['device_label'])}{tag}{seen}"


def usage_text(
    servers: list[dict], *, show_access: bool = False, unavailable: list[dict] | None = None
) -> str:
    unavailable = unavailable or []
    multi = len(servers) + len(unavailable) > 1 or bool(unavailable)
    lines: list[str] = ["📶 <b>VPN</b>"] if multi else []
    for server in servers:
        used = vpn_format.bytes_to_gb(server.get("used_bytes", 0))
        limit = vpn_format.bytes_to_gb(server.get("limit_bytes", 0))
        remaining = vpn_format.bytes_to_gb(server.get("remaining_bytes", 0))
        quota = f"{used:.1f} / {limit:.0f} ГБ (осталось {remaining:.1f} ГБ)"
        if multi:
            lines.append("")
            lines.append(f"<b>{html.escape(server_label(server))}</b>: {quota}")
        else:
            lines.append(f"📶 <b>VPN</b>: {quota}")
        health = vpn_check_view.health_line(server)
        if health:
            lines.append(health)
        if show_access and not is_allowed(server):
            lines.append("🔒 Доступ на эту локацию не открыт.")
        if server.get("blocked"):
            lines.append("⛔️ Доступ приостановлен — лимит месяца исчерпан.")
        devices = server.get("devices") or []
        if devices and not multi:
            lines.append("")
            lines.append("Устройства:")
        lines.extend(_device_line(device) for device in devices)
    for server in unavailable:
        lines.append("")
        lines.append(f"<b>{html.escape(server_label(server))}</b>: 🔌 Сервер недоступен")
    if vpn_check_view.needs_legend(servers):
        lines.append("")
        lines.append(vpn_check_view.LEGEND)
    return "\n".join(lines)


def summary_text(summary: dict) -> str:
    chats = summary.get("chats") or []
    lines = [f"📊 <b>VPN — расход за {summary.get('month', '?')}</b>"]
    node = summary.get("node") or {}
    if node:
        free = vpn_format.bytes_to_gb(node.get("free_bytes", 0))
        reserved = vpn_format.bytes_to_gb(node.get("reserved_bytes", 0))
        limit = vpn_format.bytes_to_gb(node.get("limit_bytes", 0))
        lines.append(
            f"Резерв ноды: {reserved:.0f} / {limit:.0f} ГБ занято обещаниями "
            f"гостям, свободно {free:.0f} ГБ"
        )
    if not chats:
        lines.append("")
        lines.append("Гостей с активным VPN-доступом пока нет.")
        return "\n".join(lines)
    lines.append("")
    for row in sorted(chats, key=lambda r: r["used_bytes"], reverse=True):
        used = vpn_format.bytes_to_gb(row["used_bytes"])
        limit = vpn_format.bytes_to_gb(row["limit_bytes"])
        devices = row.get("device_count")
        devices_note = f", устройств: {devices}" if devices is not None else ""
        lines.append(f"• <code>{row['chat_id']}</code>: {used:.1f} / {limit:.0f} ГБ{devices_note}")
    return "\n".join(lines)


def card_keyboard(
    servers: list[dict],
    *,
    subscription: Subscription | None,
    self_serve_nodes: list[str],
) -> InlineKeyboardMarkup:
    multi = len(servers) > 1
    devices = [device for server in servers for device in (server.get("devices") or [])]
    top_row: list[InlineKeyboardButton] = []
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_VPN_APK):
        top_row.append(vpn_keyboards.help_button())
    needy = (
        self_serve_nodes if vpn_rights.allows(subscription, vpn_protocol.ACTION_GRANT_EXTRA) else []
    )
    for server in servers:
        if server.get("node") not in needy:
            continue
        suffix = f" ({server_label(server)})" if multi else ""
        top_row.insert(0, vpn_keyboards.grant_extra_button(server.get("node"), suffix))
    rows: list[list[InlineKeyboardButton]] = [top_row] if top_row else []
    can_reissue = vpn_rights.allows(subscription, vpn_protocol.ACTION_REISSUE)
    can_revoke = vpn_rights.allows(subscription, vpn_protocol.ACTION_REVOKE)
    for device in devices:
        label = device["device_label"]
        server = device.get("server")
        device_row: list[InlineKeyboardButton] = []
        if can_reissue:
            device_row.append(
                button(
                    f"🔄 Перевыпустить «{label}»",
                    _cb.vpn_callback(vpn_protocol.ACTION_REISSUE, label, server),
                )
            )
        if can_revoke:
            device_row.append(
                button(
                    f"🗑 Отозвать «{label}»",
                    _cb.vpn_callback(vpn_protocol.ACTION_REVOKE, label, server),
                )
            )
        if device_row:
            rows.append(device_row)
    if vpn_rights.allows(subscription, vpn_protocol.ACTION_ISSUE):
        rows.append(
            [button(vpn_keyboards.NEW_DEVICE_TEXT, _cb.vpn_callback(vpn_protocol.ACTION_ISSUE))]
        )
    admin_row = vpn_screens.admin_row(servers, subscription)
    if admin_row is not None:
        rows.append(admin_row)
    proxy_row = vpn_screens.proxy_row(servers, subscription)
    if proxy_row is not None:
        rows.append(proxy_row)
    return markup(*rows)
