from __future__ import annotations

import asyncio
import base64
import contextlib
import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.ui.keyboards import single_button
from sa_home_bot.bot.vpn_secrets import PendingVpnSecret, PendingVpnSecrets
from sa_home_bot.vpn import protocol as vpn_protocol

QR_FILENAME = "vpn-qr.png"
REVEAL_QR = "qr"
REVEAL_FILE = "file"
FILE_FIRST_PROMPT = (
    "Настраиваете ДРУГОЕ устройство? Удобнее QR-кодом — нажмите ниже. "
    "Кнопка одноразовая и скоро исчезнет."
)
QR_FIRST_PROMPT = (
    "Настраиваете С ЭТОГО устройства? Удобнее файлом — нажмите ниже. "
    "Кнопка одноразовая и скоро исчезнет."
)


def _awg_file_caption(label: str) -> str:
    return (
        f"🔐 Конфиг устройства «{label}».\n"
        "Нажмите на файл → «Открыть с помощью» → AmneziaVPN — тоннель "
        "добавится сразу, без копирования."
    )


def _awg_qr_caption(label: str, flag: str) -> str:
    return (
        f"📶 QR — «{label}» · AmneziaVPN{' ' + flag if flag else ''}. Откройте AmneziaVPN → "
        "«+» → «QR-код» (удобно для настройки с ДРУГОГО "
        "устройства — сфотографировать собственный экран телефон не может)."
    )


def _reality_file_caption(label: str) -> str:
    return (
        f"⚙️ Настройки маршрутизации «{label}» (VLESS) — файл.\n"
        "Hiddify → «+» → «Из файла» → выберите этот файл, «Импортировать». "
        "Это НЕ подключение, а РФ-маршруты/DNS — импортируйте один раз при "
        "первой установке приложения на устройстве. Само подключение "
        "добавьте отдельно — ссылкой или QR ниже, без этого шага профиля "
        "не будет."
    )


def _reality_qr_caption(label: str, flag: str) -> str:
    return (
        f"📶 QR — «{label}» · VLESS · Hiddify{' ' + flag if flag else ''}. "
        "Hiddify → «+» → «Сканировать QR» (удобно для настройки с ДРУГОГО устройства)."
    )


@dataclass(frozen=True)
class _Captions:
    file: Callable[[str], str]
    qr: Callable[[str, str], str]


_CAPTIONS = {
    vpn_protocol.TRANSPORT_AWG: _Captions(_awg_file_caption, _awg_qr_caption),
    vpn_protocol.TRANSPORT_REALITY: _Captions(_reality_file_caption, _reality_qr_caption),
}


def _captions(transport: str) -> _Captions:
    return _CAPTIONS.get(transport, _CAPTIONS[vpn_protocol.TRANSPORT_AWG])


def file_caption(transport: str, label_escaped: str) -> str:
    return _captions(transport).file(label_escaped)


def qr_caption(transport: str, label_escaped: str, location: str = "") -> str:
    return _captions(transport).qr(label_escaped, html.escape(vpn_protocol.country_flag(location)))


def reality_links_note(result: dict | PendingVpnSecret) -> str:
    if isinstance(result, PendingVpnSecret):
        deep_link, share_url = result.deep_link, result.share_url
    else:
        deep_link = result.get("deep_link")
        share_url = result.get("share_url")
    parts: list[str] = []
    if deep_link:
        parts.append(f"🔗 Импорт одним нажатием: <code>{html.escape(str(deep_link))}</code>")
    if share_url:
        parts.append(f"Ссылка: <code>{html.escape(str(share_url))}</code>")
    if not parts:
        return ""
    return "\n\n📲 Это и есть подключение (нужно для каждого устройства):\n" + "\n".join(parts)


def cleanup_later(
    notifier: Notifier,
    pending: PendingVpnSecrets | None,
    chat_id: int,
    message_ids: Sequence[int | None],
    tokens: Sequence[str],
    ttl_s: float,
) -> None:
    ids = [message_id for message_id in message_ids if message_id is not None]
    if not ids and not tokens:
        return

    async def _cleanup() -> None:
        await asyncio.sleep(ttl_s)
        for token in tokens:
            if pending is not None:
                pending.discard(token)
        for message_id in ids:
            await notifier.delete_message(chat_id, message_id)

    asyncio.create_task(_cleanup(), name="vpn-secret-cleanup")


async def send_config_file(
    notifier: Notifier,
    chat_id: int,
    *,
    transport: str,
    device_label: str,
    config_text: str,
    location: str,
    caption: str,
    thread_id: int | None,
) -> int | None:
    sent = await notifier.send_document(
        chat_id,
        config_text.encode("utf-8"),
        filename=vpn_protocol.secret_filename(transport, device_label, location),
        caption=caption,
        message_thread_id=thread_id,
    )
    return sent[0] if sent is not None else None


async def send_qr(
    notifier: Notifier,
    chat_id: int,
    qr_png_b64: str,
    *,
    caption: str,
    thread_id: int | None,
    filename: str = QR_FILENAME,
) -> int | None:
    return await notifier.send_photo(
        chat_id,
        base64.b64decode(qr_png_b64),
        filename=filename,
        caption=caption,
        message_thread_id=thread_id,
    )


def get_config_keyboard(action_id: str, token: str, button_text: str) -> InlineKeyboardMarkup:
    return single_button(button_text, vpn_callbacks.secret_cb(action_id, token))


async def send_secret(
    notifier: Notifier,
    pending: PendingVpnSecrets,
    chat_id: int,
    action_id: str,
    result: dict,
    ttl_s: float,
    message_thread_id: int | None = None,
) -> None:
    device_label = str(result.get("device_label") or "")
    label_escaped = html.escape(device_label)
    config_text = str(result.get("config_text") or "")
    qr_b64 = result.get("qr_png_b64")
    transport = str(result.get("transport") or vpn_protocol.TRANSPORT_AWG)
    location = str(result.get("location") or "")
    links_note = reality_links_note(result) if transport == vpn_protocol.TRANSPORT_REALITY else ""
    file_first = int(result.get("prior_device_count") or 0) == 0

    primary_id: int | None = None
    if file_first:
        primary_id = await send_config_file(
            notifier,
            chat_id,
            transport=transport,
            device_label=device_label,
            config_text=config_text,
            location=location,
            caption=file_caption(transport, label_escaped),
            thread_id=message_thread_id,
        )
        reveal, prompt, button_text = REVEAL_QR, FILE_FIRST_PROMPT, "📶 Дать QR-код"
    else:
        if qr_b64:
            primary_id = await send_qr(
                notifier,
                chat_id,
                qr_b64,
                caption=qr_caption(transport, label_escaped, location),
                thread_id=message_thread_id,
            )
        reveal, prompt, button_text = REVEAL_FILE, QR_FIRST_PROMPT, "📄 Дать файл конфига"

    token = pending.put(
        config_text,
        device_label,
        qr_b64,
        reveal,
        ttl_s,
        transport=transport,
        share_url=result.get("share_url"),
        deep_link=result.get("deep_link"),
        location=location,
    )
    button_id = await notifier.send_direct(
        chat_id,
        prompt + links_note,
        reply_markup=get_config_keyboard(action_id, token, button_text),
        message_thread_id=message_thread_id,
    )
    cleanup_later(notifier, pending, chat_id, [primary_id, button_id], [token], ttl_s)


def without_button(message: object, callback_data: str | None) -> InlineKeyboardMarkup | None:
    markup = getattr(message, "reply_markup", None)
    rows = [
        [b for b in row if b.callback_data != callback_data]
        for row in (getattr(markup, "inline_keyboard", None) or [])
    ]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def reveal_secret(
    callback: CallbackQuery,
    notifier: Notifier,
    pending: PendingVpnSecrets,
    token: str,
    ttl_s: float,
) -> None:
    secret = pending.pop(token)
    if secret is None:
        await callback.answer("Ссылка устарела — запросите доступ ещё раз.", show_alert=True)
        return
    await callback.answer("Отправляю…")
    chat_id = callback.message.chat.id
    thread_id = callback.message.message_thread_id
    label_escaped = html.escape(secret.device_label)
    if secret.reveal == REVEAL_QR and secret.qr_png_b64:
        sent_id = await send_qr(
            notifier,
            chat_id,
            secret.qr_png_b64,
            caption=qr_caption(secret.transport, label_escaped, secret.location),
            thread_id=thread_id,
        )
    else:
        sent_id = await send_config_file(
            notifier,
            chat_id,
            transport=secret.transport,
            device_label=secret.device_label,
            config_text=secret.config_text,
            location=secret.location,
            caption=file_caption(secret.transport, label_escaped),
            thread_id=thread_id,
        )
    cleanup_later(notifier, None, chat_id, [sent_id], [], ttl_s)
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_reply_markup(
            reply_markup=without_button(callback.message, callback.data)
        )
