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
_DST = Address(node=vpn_protocol.NODE_ID, service=vpn_protocol.SERVICE_NAME)


async def _download_chunks(node_link: ServiceLink, expected_sha256: str | None) -> bytes:
    parts: list[bytes] = []
    offset = 0
    while True:
        chunk = await node_link.command(
            vpn_protocol.ACTION_APK_CHUNK,
            {"offset": offset, "length": _CHUNK_BYTES},
            dst=_DST,
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


async def deliver_apk(node_link: ServiceLink, notifier: Notifier, chat_id: int) -> str:
    """Отправить актуальный APK гостю в личку. Возвращает текст результата
    (пригоден и как ответ хендлера, и как ответ LLM-тула)."""
    try:
        info = await node_link.command(vpn_protocol.ACTION_APK_INFO, {}, dst=_DST)
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
        sent = await notifier.send_document(chat_id, str(file_id), caption=caption)
        if sent is not None:
            return "📱 Приложение отправлено."
        log.info("vpn: file_id APK устарел, качаю заново через рой")

    try:
        payload = await _download_chunks(node_link, info.get("sha256"))
    except (ProtoError, ServiceUnavailableError, TimeoutError) as exc:
        return f"недоступно: не удалось скачать APK ({exc})"
    except ValueError as exc:
        return f"не вышло: {exc}"

    filename = str(info.get("file_name") or f"AmneziaWG-{version}.apk")
    document = BufferedInputFile(payload, filename=filename)
    sent = await notifier.send_document(chat_id, document, caption=caption)
    if sent is None:
        return "не вышло: не удалось отправить файл"
    _message_id, new_file_id = sent
    if new_file_id:
        with contextlib.suppress(ProtoError, ServiceUnavailableError, TimeoutError):
            await node_link.command(
                vpn_protocol.ACTION_APK_SET_FILE_ID, {"telegram_file_id": new_file_id}, dst=_DST
            )
    return "📱 Приложение отправлено."
