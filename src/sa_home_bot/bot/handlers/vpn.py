"""/vpn — карточка своего доступа: расход, устройства, «+100 ГБ»,
перевыпуск, приложение. Админ дополнительно видит сводку по всем гостям и
решает заявки на трафик сверх потолка самообслуживания (кнопки approve/deny
приходят и из уведомления bot/node_events.py::EVENT_VPN_EXTRA_REQUESTED).

Кнопки идут по общей схеме ``act:vpn:<действие>[:<значение>]``
(commands.action_callback) — право на каждую уже проверила
CallbackAuthorizationMiddleware (``действие@vpn``), здесь только исполнение
и рендер. Диспетчеризация сюда — из bot/handlers/node.py::on_dynamic_action
(тот же приём, что apps/monitor: у "act:"-кнопок один обработчик на все
службы, разбор — по service).

Секрет (приватный ключ) уходит в личку один раз файлом ``.conf`` (тап →
«Открыть с помощью» → AmneziaWG импортирует тоннель без копирования — живая
находка 2026-08-03: текст в `<pre>` заставлял гостя вручную создавать
документ, а QR бесполезен, если сканировать нечем — телефон не может
сфотографировать собственный экран) и QR-картинкой (для настройки с ДРУГОГО
устройства), затем бот удаляет оба сообщения через
``[vpn].config_message_ttl_s`` — решение плана этапа 33: приватный ключ
генерируется на сервере, в БД служба хранит только публичный
(vpn/service.py)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import logging
import re

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sa_home_bot.bot import commands
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.vpn_apk import deliver_apk
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

router = Router(name="vpn")

SERVICE = vpn_protocol.SERVICE_NAME
_DST = Address(node=vpn_protocol.NODE_ID, service=SERVICE)

# Ярлыки устройств для кнопки «новое устройство» — короткие, чтобы влезать
# в callback_data (лимит Telegram — 64 байта на всю строку).
_DEVICE_LABELS = ("телефон", "ноутбук", "планшет", "ПК")


def _is_private(chat_id: int) -> bool:
    return chat_id > 0


def _gb(bytes_: int) -> float:
    return bytes_ / 1_000_000_000


def _usage_text(usage: dict) -> str:
    used = _gb(usage.get("used_bytes", 0))
    limit = _gb(usage.get("limit_bytes", 0))
    remaining = _gb(usage.get("remaining_bytes", 0))
    lines = [f"📶 <b>VPN</b>: {used:.1f} / {limit:.0f} ГБ (осталось {remaining:.1f} ГБ)"]
    if usage.get("blocked"):
        lines.append("⛔️ Доступ приостановлен — лимит месяца исчерпан.")
    devices = usage.get("devices") or []
    if devices:
        lines.append("")
        lines.append("Устройства:")
        for device in devices:
            handshake = device.get("last_handshake_at")
            seen = (
                f", было на связи {handshake[:16].replace('T', ' ')}"
                if handshake
                else ", ещё не подключалось"
            )
            lines.append(f"• {html.escape(device['device_label'])}{seen}")
    return "\n".join(lines)


def _summary_text(summary: dict) -> str:
    chats = summary.get("chats") or []
    if not chats:
        return "Гостей с VPN-доступом пока нет."
    lines = [f"📊 <b>VPN — расход за {summary.get('month', '?')}</b>"]
    for row in sorted(chats, key=lambda r: r["used_bytes"], reverse=True):
        used = _gb(row["used_bytes"])
        limit = _gb(row["limit_bytes"])
        lines.append(f"• <code>{row['chat_id']}</code>: {used:.1f} / {limit:.0f} ГБ")
    return "\n".join(lines)


def _card_keyboard(devices: list[dict], *, is_admin: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ 100 ГБ",
                callback_data=commands.action_callback("grant_extra", service=SERVICE),
            ),
            InlineKeyboardButton(
                text="📱 Приложение",
                callback_data=commands.action_callback("apk", service=SERVICE),
            ),
        ]
    ]
    for device in devices:
        label = device["device_label"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔄 Перевыпустить «{label}»",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_REISSUE, label, service=SERVICE
                    ),
                )
            ]
        )
    used_labels = {device["device_label"] for device in devices}
    for label in _DEVICE_LABELS:
        if label not in used_labels:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"➕ Новое устройство: {label}",
                        callback_data=commands.action_callback(
                            vpn_protocol.ACTION_ISSUE, label, service=SERVICE
                        ),
                    )
                ]
            )
            break
    if is_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    text="👥 Все гости",
                    callback_data=commands.action_callback("usage_all", service=SERVICE),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _is_admin(subscription: Subscription) -> bool:
    return subscription.allows_action(vpn_protocol.ACTION_PEERS, SERVICE)


async def _card(node_link: ServiceLink, chat_id: int) -> tuple[str, dict] | tuple[None, None]:
    try:
        usage = await node_link.command(
            vpn_protocol.ACTION_USAGE, {"chat_id": chat_id}, dst=_DST
        )
    except ServiceUnavailableError:
        return "⚠️ Служба VPN недоступна — попробуйте позже.", None
    except ProtoError as exc:
        return f"⚠️ Ошибка: {exc.message}", None
    return None, usage


@router.message(Command(commands.VPN.name))
async def cmd_vpn(
    message: Message, node_link: ServiceLink, subscription: Subscription | None = None
) -> None:
    error, usage = await _card(node_link, message.chat.id)
    if error is not None:
        await message.answer(error)
        return
    is_admin = subscription is not None and _is_admin(subscription)
    keyboard = _card_keyboard(usage.get("devices") or [], is_admin=is_admin)
    await message.answer(_usage_text(usage), reply_markup=keyboard)


async def _redraw_card(
    callback: CallbackQuery, node_link: ServiceLink, subscription: Subscription
) -> None:
    error, usage = await _card(node_link, callback.message.chat.id)
    if error is not None:
        return
    keyboard = _card_keyboard(usage.get("devices") or [], is_admin=_is_admin(subscription))
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(_usage_text(usage), reply_markup=keyboard)


_UNSAFE_FILENAME = re.compile(r"[^\w\-]+")


def _safe_filename(label: str) -> str:
    return _UNSAFE_FILENAME.sub("_", label.strip()).strip("_") or "device"


async def _send_secret(
    notifier: Notifier,
    chat_id: int,
    result: dict,
    ttl_s: float,
    message_thread_id: int | None = None,
) -> None:
    device_label = html.escape(str(result.get("device_label") or ""))
    config_text = str(result.get("config_text") or "")
    filename = f"amneziawg-{_safe_filename(str(result.get('device_label') or 'device'))}.conf"
    doc_sent = await notifier.send_document(
        chat_id,
        config_text.encode("utf-8"),
        filename=filename,
        caption=(
            f"🔐 Конфиг устройства «{device_label}».\n"
            "Нажмите на файл → «Открыть с помощью» → AmneziaWG — тоннель "
            "добавится сразу, без копирования. Если настраиваете НЕ с этого "
            "устройства — ниже есть QR для сканирования камерой. Удалите "
            "переписку после импорта — сообщения исчезнут сами через "
            "несколько минут."
        ),
        message_thread_id=message_thread_id,
    )
    doc_id = doc_sent[0] if doc_sent is not None else None
    photo_id = None
    qr_b64 = result.get("qr_png_b64")
    if qr_b64:
        photo_id = await notifier.send_photo(
            chat_id,
            base64.b64decode(qr_b64),
            filename="vpn-qr.png",
            caption=f"QR — «{device_label}» (для настройки с другого устройства)",
            message_thread_id=message_thread_id,
        )

    async def _cleanup() -> None:
        await asyncio.sleep(ttl_s)
        if doc_id is not None:
            await notifier.delete_message(chat_id, doc_id)
        if photo_id is not None:
            await notifier.delete_message(chat_id, photo_id)

    asyncio.create_task(_cleanup(), name="vpn-secret-cleanup")


async def handle_action(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
) -> None:
    """Вызывается из bot/handlers/node.py::on_dynamic_action для service="vpn"."""
    parsed = commands.parse_action_callback(callback.data)
    if parsed is None or callback.message is None:
        await callback.answer()
        return
    _service, action_id, value, _node_id = parsed
    chat_id = callback.message.chat.id

    if action_id == "apk":
        if not _is_private(chat_id):
            await callback.answer("Напишите мне в личку — там и пришлю.", show_alert=True)
            return
        await callback.answer("Отправляю…")
        text = await deliver_apk(
            node_link, notifier, chat_id, message_thread_id=callback.message.message_thread_id
        )
        await callback.message.answer(text)
        return

    if action_id == vpn_protocol.ACTION_GRANT_EXTRA:
        try:
            await node_link.command(vpn_protocol.ACTION_GRANT_EXTRA, {"chat_id": chat_id}, dst=_DST)
        except ProtoError as exc:
            if exc.code == vpn_protocol.ERR_QUOTA_CEILING:
                result = await node_link.command(
                    vpn_protocol.ACTION_REQUEST_EXTRA, {"chat_id": chat_id}, dst=_DST
                )
                await callback.answer()
                await callback.message.answer(
                    f"✋ Потолок самообслуживания достигнут — заявка №{result['request_id']} "
                    "отправлена админу."
                )
                return
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer("Добавлено")
        await _redraw_card(callback, node_link, subscription)
        return

    if action_id in (vpn_protocol.ACTION_ISSUE, vpn_protocol.ACTION_REISSUE):
        if not _is_private(chat_id):
            await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
            return
        if not value:
            await callback.answer()
            return
        await callback.answer("Выпускаю…")
        try:
            result = await node_link.command(
                action_id, {"chat_id": chat_id, "device_label": value}, dst=_DST
            )
        except ProtoError as exc:
            await callback.message.answer(f"⚠️ {exc.message}")
            return
        except ServiceUnavailableError:
            await callback.message.answer("⚠️ Служба VPN недоступна.")
            return
        await _send_secret(
            notifier,
            chat_id,
            result,
            config.vpn.config_message_ttl_s,
            message_thread_id=callback.message.message_thread_id,
        )
        await _redraw_card(callback, node_link, subscription)
        return

    if action_id == "usage_all":
        try:
            summary = await node_link.command(vpn_protocol.ACTION_USAGE, {}, dst=_DST)
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(_summary_text(summary))
        return

    if action_id == vpn_protocol.ACTION_RESOLVE_REQUEST:
        if not value or "_" not in value:
            await callback.answer()
            return
        raw_id, _, decision = value.partition("_")
        approve = decision == "approve"
        try:
            result = await node_link.command(
                vpn_protocol.ACTION_RESOLVE_REQUEST,
                {"request_id": int(raw_id), "approve": approve},
                dst=_DST,
            )
        except (ProtoError, ServiceUnavailableError, ValueError) as exc:
            await callback.answer(f"⚠️ {exc}", show_alert=True)
            return
        await callback.answer("Одобрено" if approve else "Отклонено")
        await callback.message.answer(
            f"Заявка №{result['request_id']} — {'✅ одобрена' if approve else '🚫 отклонена'}."
        )
        return

    await callback.answer()


def resolve_request_callback(request_id: int) -> InlineKeyboardMarkup:
    """Кнопки approve/deny под уведомлением админу о заявке на трафик
    (bot/node_events.py::EVENT_VPN_EXTRA_REQUESTED)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_RESOLVE_REQUEST,
                        f"{request_id}_approve",
                        service=SERVICE,
                    ),
                ),
                InlineKeyboardButton(
                    text="🚫 Отклонить",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_RESOLVE_REQUEST, f"{request_id}_deny", service=SERVICE
                    ),
                ),
            ]
        ]
    )
