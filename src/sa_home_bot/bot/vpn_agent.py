from __future__ import annotations

import html
from typing import Any

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_format, vpn_help, vpn_nodes, vpn_report, vpn_settings, vpn_wizard

MAX_REASON = vpn_report.MAX_DESCRIPTION


async def servers_for(node_link: Any, chat_id: int) -> tuple[list[dict], list[dict]]:
    answered = await vpn_nodes.usage_for_chat(node_link, chat_id)
    return vpn_nodes.allowed_servers(answered), answered


def _plain(text: str) -> str:
    return html.unescape(text)


def status_text(servers: list[dict]) -> str:
    if not servers:
        return "VPN не открыт: ни одной страны для этого человека."
    lines = [_plain(line) for line in vd.home_summary(servers)]
    devices = vd.build_devices(servers)
    lines.append("")
    if not devices:
        lines.append("Устройств пока нет.")
    for dev in devices:
        if dev.never_connected:
            tail = "ещё не подключалось"
        else:
            traffic = vpn_format.format_gb(dev.used_bytes)
            tail = f"{traffic}, на связи {vpn_format.format_stamp(dev.last_handshake_at)}"
        broken = " (сервер не помнит ключ — нужен перевыпуск)" if dev.broken_nodes() else ""
        lines.append(f"Устройство «{dev.label}» — {tail}{broken}")
    return "\n".join(lines)


def wizard_available(servers: list[dict]) -> bool:
    return vpn_nodes.has_reality_server(servers)


async def send_wizard(notifier: Any, chat_id: int, thread_id: int | None) -> int | None:
    return await notifier.send_direct(
        chat_id,
        vpn_wizard.INTRO_TEXT,
        reply_markup=vpn_wizard.intro_keyboard(),
        message_thread_id=thread_id,
    )


def find_device(servers: list[dict], label: str) -> vd.Device | None:
    devices = vd.build_devices(servers)
    wanted = label.strip().casefold()
    for dev in devices:
        if dev.label.casefold() == wanted:
            return dev
    if not wanted and len(devices) == 1:
        return devices[0]
    return None


async def send_settings(
    notifier: Any, chat_id: int, thread_id: int | None, device: vd.Device
) -> int | None:
    return await notifier.send_direct(
        chat_id,
        vpn_settings.pick_text(device.label),
        reply_markup=vpn_settings.pick_keyboard(device.key),
        message_thread_id=thread_id,
    )


async def submit_report(
    node_link: Any,
    notifier: Any,
    book: Any,
    *,
    chat_id: int,
    name: str,
    username: str | None,
    reason: str,
    servers: list[dict],
    answered: list[dict],
    device_key: str = "",
) -> vpn_help.Outcome:
    blocked = vpn_help.blocked_outcome(book, chat_id)
    if blocked is not None:
        return blocked
    text = html.escape(reason.strip()[:MAX_REASON]) + " (со слов человека, через Альфреда)"
    return await vpn_help.send_help_request(
        book,
        notifier,
        chat_id=chat_id,
        who=vpn_help.who_text(name, username),
        reason=text,
        context=vpn_help.context_or_empty(servers, answered, device_key),
    )
