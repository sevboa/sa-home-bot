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

Секрет (приватный ключ) уходит в личку дважды, но порядок способов зависит
от того, первое ли это устройство чата (решение пользователя 2026-08-04,
критерий — vpn/service.py::_issue → ``prior_device_count``):

- первое устройство чата — почти наверняка настраивается ПРЯМО С ЭТОГО
  телефона, поэтому сразу уходит файл ``.conf`` (тап на файл → «Открыть с
  помощью» → AmneziaWG импортирует тоннель без копирования — живая находка
  2026-08-03 про сам механизм импорта), а QR — по кнопке, если всё же нужно
  перенести на другое устройство;
- второе и последующие устройства чата — скорее всего заводятся ДЛЯ ДРУГОГО
  устройства (или человека), поэтому сразу уходит QR (сканируется камерой
  прямо из приложения), а файл — по кнопке.

Кнопка одна и исчезает после использования. Приватный ключ до этого момента
нигде не хранится: ни в БД (vpn/service.py и так хранит только публичный),
ни в callback_data (лимит Telegram — 64 байта, весь конфиг туда не влезает)
— он живёт в памяти процесса ровно до выдачи или до
``[vpn].config_message_ttl_s`` (bot/vpn_secrets.py::PendingVpnSecrets), когда
оба сообщения (первый способ и кнопка, а если второй способ всё же
попросили — то и он) бот удаляет сам."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import logging
import re
import time

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
from sa_home_bot.bot.vpn_secrets import PendingVpnSecrets
from sa_home_bot.config import Settings
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

log = logging.getLogger(__name__)

router = Router(name="vpn")

SERVICE = vpn_protocol.SERVICE_NAME
_DST = Address(node=vpn_protocol.NODE_ID, service=SERVICE)


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
    lines = [f"📊 <b>VPN — расход за {summary.get('month', '?')}</b>"]
    node = summary.get("node") or {}
    if node:
        free = _gb(node.get("free_bytes", 0))
        reserved = _gb(node.get("reserved_bytes", 0))
        limit = _gb(node.get("limit_bytes", 0))
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
        used = _gb(row["used_bytes"])
        limit = _gb(row["limit_bytes"])
        devices = row.get("device_count")
        devices_note = f", устройств: {devices}" if devices is not None else ""
        lines.append(f"• <code>{row['chat_id']}</code>: {used:.1f} / {limit:.0f} ГБ{devices_note}")
    return "\n".join(lines)


def _card_keyboard(
    devices: list[dict], *, is_admin: bool, can_self_serve: bool
) -> InlineKeyboardMarkup:
    top_row = [
        InlineKeyboardButton(
            text="📱 Приложение",
            callback_data=commands.action_callback("apk", service=SERVICE),
        ),
    ]
    if can_self_serve:
        # Кнопка появляется, только когда самообслуживание реально доступно
        # (см. vpn/service.py::_grant_extra) — иначе гость с почти полной
        # квотой жал бы её впустую и получал отказ вместо понятной картины.
        top_row.insert(
            0,
            InlineKeyboardButton(
                text="➕ 100 ГБ",
                callback_data=commands.action_callback("grant_extra", service=SERVICE),
            ),
        )
    rows: list[list[InlineKeyboardButton]] = [top_row]
    for device in devices:
        label = device["device_label"]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔄 Перевыпустить «{label}»",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_REISSUE, label, service=SERVICE
                    ),
                ),
                InlineKeyboardButton(
                    text=f"🗑 Отозвать «{label}»",
                    callback_data=commands.action_callback(
                        vpn_protocol.ACTION_REVOKE, label, service=SERVICE
                    ),
                ),
            ]
        )
    # Имя устройства служба выбирает сама — случайный цветок (решение
    # пользователя 2026-08-04, см. vpn/service.py::_random_device_label) —
    # кнопке больше нечего предлагать заранее, число устройств не
    # ограничено, поэтому она всего одна и всегда доступна.
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Новое устройство",
                callback_data=commands.action_callback(vpn_protocol.ACTION_ISSUE, service=SERVICE),
            )
        ]
    )
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


# Обе ссылки в строке текстом (не длинным URL): решение пользователя
# 2026-08-04, "на аппстор обе и на гугл плей обе" — по магазину, а не по
# приложению, чтобы у AmneziaWG остался явный официальный путь на iOS
# (сайдлоада там нет вовсе).
def _store_row(emoji: str, store: str, vpn_url: str, wg_url: str) -> str:
    return (
        f'{emoji} {store}: <a href="{html.escape(vpn_url)}">AmneziaVPN</a> · '
        f'<a href="{html.escape(wg_url)}">AmneziaWG</a>'
    )


def _apk_links_text(config: Settings) -> str:
    cfg = config.vpn
    return (
        "📱 Настоятельно рекомендуем полную версию — <b>AmneziaVPN</b>. Есть и "
        "облегчённая — <b>AmneziaWG</b> (её и использует эта настройка).\n\n"
        + _store_row("🍎", "App Store", cfg.amneziavpn_ios_app_store_url, cfg.ios_app_store_url)
        + "\n"
        + _store_row("🤖", "Google Play", cfg.amneziavpn_google_play_url, cfg.google_play_url)
        + "\n"
        f"🌐 Официальный сайт (все платформы, обе версии): "
        f"{html.escape(cfg.official_download_url)}\n\n"
        "Если магазины недоступны — можно получить файл .apk облегчённой версии "
        "прямо здесь, кнопка ниже. Это аварийный способ на случай, если "
        "официальные способы не сработали, а не замена им."
    )


def _apk_links_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Дать APK",
                    callback_data=commands.action_callback("apk", "send", service=SERVICE),
                )
            ]
        ]
    )


def _is_admin(subscription: Subscription) -> bool:
    return subscription.allows_action(vpn_protocol.ACTION_PEERS, SERVICE)


async def usage_text(node_link: ServiceLink, chat_id: int) -> str:
    """Расход VPN произвольного чата — текстом, без клавиатуры.

    Переиспользуется карточкой гостя в /guests (кнопка «Статистика VPN»):
    та же служба и то же действие usage@vpn, что и у команды /vpn, только для
    чужого chat_id — админ имеет право знать расход того, кем управляет.
    """
    error, usage = await _card(node_link, chat_id)
    return error if error is not None else _usage_text(usage)


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


def _can_self_serve(usage: dict, config: Settings) -> bool:
    threshold = config.vpn.warn_remaining_gb * 1_000_000_000
    return usage.get("remaining_bytes", 0) <= threshold


@router.message(Command(commands.VPN.name))
async def cmd_vpn(
    message: Message,
    node_link: ServiceLink,
    config: Settings,
    subscription: Subscription | None = None,
) -> None:
    error, usage = await _card(node_link, message.chat.id)
    if error is not None:
        await message.answer(error)
        return
    is_admin = subscription is not None and _is_admin(subscription)
    keyboard = _card_keyboard(
        usage.get("devices") or [], is_admin=is_admin, can_self_serve=_can_self_serve(usage, config)
    )
    await message.answer(_usage_text(usage), reply_markup=keyboard)


async def _redraw_card(
    callback: CallbackQuery, node_link: ServiceLink, subscription: Subscription, config: Settings
) -> None:
    error, usage = await _card(node_link, callback.message.chat.id)
    if error is not None:
        return
    keyboard = _card_keyboard(
        usage.get("devices") or [],
        is_admin=_is_admin(subscription),
        can_self_serve=_can_self_serve(usage, config),
    )
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(_usage_text(usage), reply_markup=keyboard)


_UNSAFE_FILENAME = re.compile(r"[^a-z0-9]+")
_MAX_TUNNEL_NAME = 15  # NAME_PATTERN wireguard-android: [a-zA-Z0-9_=+.-]{1,15}


def _conf_filename(device_label: str) -> str:
    """Имя тоннеля в .conf = имя файла без расширения — приложения на базе
    wireguard-android валидируют его по ``[a-zA-Z0-9_=+.-]{1,15}`` (см.
    NAME_PATTERN в исходниках). device_label теперь всегда английское слово
    из фиксированного пула (vpn/service.py::_random_device_label) —
    транслитерация больше не нужна, только нормализация регистра и знак
    подчёркивания перед меткой времени (решение пользователя 2026-08-04:
    только английские символы и "_", метка времени в конце — отличает
    разные выпуски одного и того же имени друг от друга)."""
    slug = _UNSAFE_FILENAME.sub("", device_label.strip().lower()) or "device"
    stamp = str(int(time.time()))[-6:]
    budget = _MAX_TUNNEL_NAME - len(stamp) - 1  # "_" между слагом и меткой
    return f"{slug[:budget]}_{stamp}.conf"


def _file_caption(label_escaped: str) -> str:
    return (
        f"🔐 Конфиг устройства «{label_escaped}».\n"
        "Нажмите на файл → «Открыть с помощью» → AmneziaWG — тоннель "
        "добавится сразу, без копирования."
    )


def _qr_caption(label_escaped: str) -> str:
    return (
        f"📶 QR — устройство «{label_escaped}». Откройте AmneziaWG → "
        "«+» → «Сканировать QR-код» (удобно для настройки с ДРУГОГО "
        "устройства — сфотографировать собственный экран телефон не может)."
    )


async def _send_secret(
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

    # Первое устройство чата — почти наверняка настраивается прямо с этого
    # телефона (файл удобнее), второе и далее — обычно для другого устройства
    # или человека (QR удобнее). См. докстринг файла и
    # vpn/service.py::_issue::prior_device_count.
    file_first = int(result.get("prior_device_count") or 0) == 0

    primary_id: int | None
    if file_first:
        sent = await notifier.send_document(
            chat_id,
            config_text.encode("utf-8"),
            filename=_conf_filename(device_label),
            caption=_file_caption(label_escaped),
            message_thread_id=message_thread_id,
        )
        primary_id = sent[0] if sent is not None else None
        reveal = "qr"
        prompt = (
            "Настраиваете ДРУГОЕ устройство? Удобнее QR-кодом — нажмите ниже. "
            "Кнопка одноразовая и скоро исчезнет."
        )
        button_text = "📶 Дать QR-код"
    else:
        primary_id = None
        if qr_b64:
            primary_id = await notifier.send_photo(
                chat_id,
                base64.b64decode(qr_b64),
                filename="vpn-qr.png",
                caption=_qr_caption(label_escaped),
                message_thread_id=message_thread_id,
            )
        reveal = "file"
        prompt = (
            "Настраиваете С ЭТОГО устройства? Удобнее файлом — нажмите ниже. "
            "Кнопка одноразовая и скоро исчезнет."
        )
        button_text = "📄 Дать файл конфига"

    token = pending.put(config_text, device_label, qr_b64, reveal, ttl_s)
    button_id = await notifier.send_direct(
        chat_id,
        prompt,
        reply_markup=_get_config_keyboard(action_id, token, button_text),
        message_thread_id=message_thread_id,
    )

    async def _cleanup() -> None:
        await asyncio.sleep(ttl_s)
        pending.discard(token)
        if primary_id is not None:
            await notifier.delete_message(chat_id, primary_id)
        if button_id is not None:
            await notifier.delete_message(chat_id, button_id)

    asyncio.create_task(_cleanup(), name="vpn-secret-cleanup")


def _get_config_keyboard(action_id: str, token: str, button_text: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=button_text,
                    # action_id — тот же, что выдал секрет (issue/reissue):
                    # право на кнопку то же самое (issue@vpn/reissue@vpn),
                    # заводить отдельное право под "забрать уже выданное"
                    # незачем (тот же приём, что apk/apk:send).
                    callback_data=commands.action_callback(
                        action_id, f"f_{token}", service=SERVICE
                    ),
                )
            ]
        ]
    )


async def handle_action(
    callback: CallbackQuery,
    node_link: ServiceLink,
    notifier: Notifier,
    config: Settings,
    subscription: Subscription,
    pending_vpn_secrets: PendingVpnSecrets,
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
        if value == "send":
            # Второе нажатие — «📦 Дать APK» под сообщением со ссылками: сам
            # файл шлём только теперь, а не сразу на первое нажатие (решение
            # пользователя 2026-08-04 — на iOS сайдлоада нет вовсе, апстор
            # надёжнее любого .apk). Кнопка прячется — второй раз файл не
            # переспросить с того же сообщения по ошибке.
            await callback.answer("Отправляю…")
            text = await deliver_apk(
                node_link, notifier, chat_id, message_thread_id=callback.message.message_thread_id
            )
            await callback.message.answer(text)
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_reply_markup(reply_markup=None)
            return
        await callback.answer()
        await callback.message.answer(_apk_links_text(config), reply_markup=_apk_links_keyboard())
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
        await _redraw_card(callback, node_link, subscription, config)
        return

    if action_id in (vpn_protocol.ACTION_ISSUE, vpn_protocol.ACTION_REISSUE):
        if not _is_private(chat_id):
            await callback.answer("Секрет доступа выдаётся только в личке.", show_alert=True)
            return
        if value and value.startswith("f_"):
            # Кнопка под уже отправленным первым способом (файл или QR) —
            # секрет ждёт в памяти (bot/vpn_secrets.py), а не в БД и не в
            # самом callback_data (см. докстринг файла). ``reveal`` говорит,
            # какой из двух способов отдать теперь.
            secret = pending_vpn_secrets.pop(value[2:])
            if secret is None:
                await callback.answer(
                    "Ссылка устарела — запросите доступ ещё раз.", show_alert=True
                )
                return
            await callback.answer("Отправляю…")
            label_escaped = html.escape(secret.device_label)
            if secret.reveal == "qr" and secret.qr_png_b64:
                await notifier.send_photo(
                    chat_id,
                    base64.b64decode(secret.qr_png_b64),
                    filename="vpn-qr.png",
                    caption=_qr_caption(label_escaped),
                    message_thread_id=callback.message.message_thread_id,
                )
            else:
                await notifier.send_document(
                    chat_id,
                    secret.config_text.encode("utf-8"),
                    filename=_conf_filename(secret.device_label),
                    caption=_file_caption(label_escaped),
                    message_thread_id=callback.message.message_thread_id,
                )
            with contextlib.suppress(TelegramBadRequest):
                await callback.message.edit_reply_markup(reply_markup=None)
            return
        if action_id == vpn_protocol.ACTION_REISSUE and not value:
            await callback.answer()
            return
        await callback.answer("Выпускаю…")
        payload = {"chat_id": chat_id}
        if value:
            payload["device_label"] = value
        try:
            result = await node_link.command(action_id, payload, dst=_DST)
        except ProtoError as exc:
            await callback.message.answer(f"⚠️ {exc.message}")
            return
        except ServiceUnavailableError:
            await callback.message.answer("⚠️ Служба VPN недоступна.")
            return
        await _send_secret(
            notifier,
            pending_vpn_secrets,
            chat_id,
            action_id,
            result,
            config.vpn.config_message_ttl_s,
            message_thread_id=callback.message.message_thread_id,
        )
        await _redraw_card(callback, node_link, subscription, config)
        return

    if action_id == vpn_protocol.ACTION_REVOKE:
        if not value:
            await callback.answer()
            return
        try:
            await node_link.command(
                vpn_protocol.ACTION_REVOKE, {"chat_id": chat_id, "device_label": value}, dst=_DST
            )
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        except ServiceUnavailableError:
            await callback.answer("⚠️ Служба VPN недоступна.", show_alert=True)
            return
        await callback.answer(f"Отозвано: «{value}»")
        await _redraw_card(callback, node_link, subscription, config)
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
