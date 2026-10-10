"""Действия ``tool_vpn`` Альфреда на новой модели устройств (этап 57.8).

Тул и кнопки /vpn — два входа в одно и то же: статус считается из
``vpn_devices``, «подключить» и «дать настройки» присылают человеку те же
экраны с кнопками (мастер ``vpn_wizard``, «📥 Получить настройки»
``vpn_settings``), «сообщить о проблеме» идёт через ``vpn_help`` тем же
форматом и с тем же троттлингом. Секреты (ключи, ссылки vless, QR) здесь не
проходят вообще: их выдают только кнопки, сообщениями бота с удалением по TTL.
Модулю aiogram нужен ради клавиатур, поэтому tools.py импортирует его лениво.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_help, vpn_nodes, vpn_report, vpn_settings, vpn_wizard
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

MAX_REASON = vpn_report.MAX_DESCRIPTION


async def servers_for(node_link: Any, chat_id: int) -> tuple[list[dict], list[dict]]:
    """Свежий usage человека по всем нодам: (открытые ему сервера, ответившие все)."""
    answered = await vpn_nodes.fanout(node_link, vpn_protocol.ACTION_USAGE, {"chat_id": chat_id})
    vpn_nodes.remember_servers(answered)
    return [s for s in answered if s.get("allowed", True)], answered


def _plain(text: str) -> str:
    return html.unescape(text)


def status_text(servers: list[dict]) -> str:
    """Статус для модели: страны ✅/⚠️, остаток квоты, устройства (трафик и связь).
    Только то, что видно на экране /vpn, — без ключей и ссылок."""
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
            tail = f"{vd.fmt_gb(dev.used_bytes)}, на связи {vd.fmt_stamp(dev.last_handshake_at)}"
        broken = " (сервер не помнит ключ — нужен перевыпуск)" if dev.broken_nodes() else ""
        lines.append(f"Устройство «{dev.label}» — {tail}{broken}")
    return "\n".join(lines)


def wizard_available(servers: list[dict]) -> bool:
    """Тот же критерий, что у /vpn: есть открытая страна с VLESS."""
    return any(vpn_protocol.TRANSPORT_REALITY in (s.get("transports") or []) for s in servers)


async def send_wizard(notifier: Any, chat_id: int, thread_id: int | None) -> int | None:
    """Первый экран мастера — сообщением, дальше человек ведёт себя кнопками."""
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
    """Экран «📥 Получить настройки» устройства — как кнопка на его карточке."""
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
    """Заявка владельцу — тот же формат и троттлинг, что у «⚠️ Сообщить о проблеме».
    ``reason`` — слова человека, экранируются здесь."""
    if chat_id == 0:  # пробник, не человек
        return "nobody"
    text = html.escape(reason.strip()[:MAX_REASON]) + " (со слов человека, через Альфреда)"
    try:
        down = vpn_nodes.unavailable_servers(answered)
        context = vpn_report.context_lines(servers, down, device_key)
    except Exception:  # noqa: BLE001 — контекст не стоит срыва заявки
        log.warning("Контекст заявки по VPN не собрался (chat=%s)", chat_id, exc_info=True)
        context = []
    return await vpn_help.send_help_request(
        book,
        notifier,
        chat_id=chat_id,
        who=vpn_help.who_text(name, username),
        reason=text,
        context=context,
    )
