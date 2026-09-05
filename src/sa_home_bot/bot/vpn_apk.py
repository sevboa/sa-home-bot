"""Доставка официального APK AmneziaWG гостю — общий код для команды /vpn
(bot/handlers/vpn.py) и LLM-тула (bot/tools.py::tool_vpn).

Метаданные (версия/размер/sha256/уже загруженный в Telegram file_id) идут
через рой обычной командой; сами байты — только если file_id ещё нет
(новая версия APK) или Telegram отверг прежний file_id (протух, например,
при смене токена бота): тогда файл едет чанками по ``_CHUNK_BYTES`` — с
запасом от MAX_MESSAGE_BYTES протокола роя (1 МиБ, proto/messages.py:
base64 раздувает байты на треть, плюс сам конверт).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import logging

from aiogram.types import BufferedInputFile

from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

_CHUNK_BYTES = 700 * 1024


async def _download_chunks(
    node_link: ServiceLink, dst: Address, expected_sha256: str | None
) -> bytes:
    parts: list[bytes] = []
    offset = 0
    while True:
        chunk = await node_link.command(
            vpn_protocol.ACTION_APK_CHUNK,
            {"offset": offset, "length": _CHUNK_BYTES},
            dst=dst,
        )
        data = base64.b64decode(chunk["data_b64"])
        if not data:
            break
        parts.append(data)
        offset += len(data)
        if chunk.get("eof"):
            break
    payload = b"".join(parts)
    if expected_sha256 and hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise ValueError("скачанный APK не прошёл проверку целостности (sha256)")
    return payload


async def deliver_apk(
    node_link: ServiceLink,
    notifier: Notifier,
    chat_id: int,
    dst: Address,
    message_thread_id: int | None = None,
) -> str:
    """Отправить актуальный APK гостю в личку. Возвращает текст результата
    (пригоден и как ответ хендлера, и как ответ LLM-тула).

    ``dst`` — адрес службы ``vpn`` (ноду выбирает bot/vpn_nodes.py; у
    каждого VPN-сервера свой кэш APK).

    ``message_thread_id`` — топик, откуда пришёл запрос (Private Chat
    Topics): без него документ уезжает в общий топик чата, а не туда, где
    пользователь реально смотрит переписку (живой баг 2026-08-03 — файл
    реально отправлялся и получал file_id, но был не виден в топике)."""
    try:
        info = await node_link.command(vpn_protocol.ACTION_APK_INFO, {}, dst=dst)
    except ProtoError as exc:
        return f"не вышло: {exc.message}"
    except (ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: VPN-служба не отвечает ({exc})"

    version = str(info.get("version") or "")
    caption = f"AmneziaWG {version}".strip()
    if info.get("stale"):
        caption += " (из кэша — GitHub сейчас недоступен для проверки версии)"

    file_id = info.get("telegram_file_id")
    if file_id:
        sent = await notifier.send_document(
            chat_id, str(file_id), caption=caption, message_thread_id=message_thread_id
        )
        if sent is not None:
            return "📱 Приложение отправлено."
        log.info("vpn: file_id APK устарел, качаю заново через рой")

    try:
        payload = await _download_chunks(node_link, dst, info.get("sha256"))
    except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: не удалось скачать APK ({exc})"
    except ValueError as exc:
        return f"не вышло: {exc}"

    filename = str(info.get("file_name") or f"AmneziaWG-{version}.apk")
    document = BufferedInputFile(payload, filename=filename)
    sent = await notifier.send_document(
        chat_id, document, caption=caption, message_thread_id=message_thread_id
    )
    if sent is None:
        return "не вышло: не удалось отправить файл"
    _message_id, new_file_id = sent
    if new_file_id:
        with contextlib.suppress(ProtoError, ServiceUnavailableError, TimeoutError):
            await node_link.command(
                vpn_protocol.ACTION_APK_SET_FILE_ID, {"telegram_file_id": new_file_id}, dst=dst
            )
    return "📱 Приложение отправлено."
