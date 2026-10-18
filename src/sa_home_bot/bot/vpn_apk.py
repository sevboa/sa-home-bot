from __future__ import annotations

import contextlib
import logging

from aiogram.types import BufferedInputFile

from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.chunks import download_chunks
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

APK_INTEGRITY_ERROR = "скачанный APK не прошёл проверку целостности (sha256)"


async def deliver_apk(
    node_link: ServiceLink,
    notifier: Notifier,
    chat_id: int,
    dst: Address,
    message_thread_id: int | None = None,
) -> str:
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
        payload = await download_chunks(
            node_link,
            vpn_protocol.ACTION_APK_CHUNK,
            dst,
            expected_sha256=info.get("sha256"),
            integrity_error=APK_INTEGRITY_ERROR,
        )
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
