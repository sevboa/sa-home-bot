"""Приём .torrent-файлов и magnet-ссылок в чате.

Файл/ссылка → карточка «куда сохранить?» с кнопками директорий из describe
службы torrents (`save_path`, choices) → по нажатию команда `add` уходит
службе. Бот в систему не ходит — файл гонится по протоколу как base64-строка
(ActionParam — только string|int|float|bool, PROTOCOL.md); обычные
.torrent-метафайлы на порядки меньше лимита сообщения (MAX_MESSAGE_BYTES =
1 МиБ, proto/messages.py), раздутие исключено.

Права не через AuthorizationMiddleware (она разбирает только текстовые
команды и «act:»-кнопки) — проверяются здесь же, как в apps.py.
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import unquote

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sa_home_bot.bot.middlewares import DENIED_TEXT
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.torrent_pending import PendingTorrent, PendingTorrents
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="torrents")

SERVICE = "torrents"
ACTION_ADD = "add"
CALLBACK_PREFIX = "tor"

_MAGNET_RE = re.compile(r"(?i)^magnet:\?")
_MAGNET_NAME_RE = re.compile(r"(?i)[?&]dn=([^&]+)")


def _is_torrent_document(message: Message) -> bool:
    doc = message.document
    if doc is None:
        return False
    name_ok = (doc.file_name or "").lower().endswith(".torrent")
    mime_ok = doc.mime_type == "application/x-bittorrent"
    return name_ok or mime_ok


def _magnet_name(magnet: str) -> str:
    m = _MAGNET_NAME_RE.search(magnet)
    return unquote(m.group(1)) if m else "magnet-ссылка"


async def _save_path_choices(link: ServiceLink) -> list[str]:
    for action in await link.actions():
        if action.id != ACTION_ADD:
            continue
        for param in action.params:
            if param.name == "save_path" and param.choices:
                return list(param.choices)
    return []


def _dir_keyboard(token: str, choices: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=path.removeprefix("/mnt/"),
            callback_data=f"{CALLBACK_PREFIX}:{token}:{idx}",
        )
        for idx, path in enumerate(choices)
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    )


def _unavailable_text(link: ServiceLink) -> str:
    if not link.connected:
        return f"⚠️ Служба «{link.display_name}» недоступна — попробуйте позже."
    return "⚠️ Умение недоступно (нет директорий для сохранения)."


async def _ask_directory(
    message: Message, torrents_link: ServiceLink, pending: PendingTorrents, source: str, name: str
) -> None:
    choices = await _save_path_choices(torrents_link)
    if not choices:
        await message.answer(_unavailable_text(torrents_link))
        return
    token = pending.add(PendingTorrent(source=source, name=name))
    await message.answer(
        f"📄 <b>{name}</b> — куда сохранить?", reply_markup=_dir_keyboard(token, choices)
    )


@router.message(F.document)
async def cmd_torrent_file(
    message: Message,
    torrents_link: ServiceLink,
    pending_torrents: PendingTorrents,
    subscription: Subscription | None = None,
) -> None:
    if not _is_torrent_document(message):
        return  # не .torrent — не наш файл, игнор
    if subscription is None or not subscription.allows_action(ACTION_ADD, SERVICE):
        await message.answer(DENIED_TEXT)
        return
    buf = await message.bot.download(message.document)
    source = base64.b64encode(buf.read()).decode()
    name = message.document.file_name or "torrent-файл"
    await _ask_directory(message, torrents_link, pending_torrents, source, name)


@router.message(F.text.regexp(_MAGNET_RE))
async def cmd_torrent_magnet(
    message: Message,
    torrents_link: ServiceLink,
    pending_torrents: PendingTorrents,
    subscription: Subscription | None = None,
) -> None:
    if subscription is None or not subscription.allows_action(ACTION_ADD, SERVICE):
        await message.answer(DENIED_TEXT)
        return
    magnet = message.text.strip()
    await _ask_directory(message, torrents_link, pending_torrents, magnet, _magnet_name(magnet))


@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
async def cb_torrent_save(
    callback: CallbackQuery,
    torrents_link: ServiceLink,
    pending_torrents: PendingTorrents,
    subscription: Subscription | None = None,
) -> None:
    if subscription is None or not subscription.allows_action(ACTION_ADD, SERVICE):
        await callback.answer(DENIED_TEXT, show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or callback.message is None:
        await callback.answer()
        return
    _, token, idx_raw = parts
    item = pending_torrents.pop(token)
    if item is None:
        await callback.answer("Устарело — пришлите файл заново.", show_alert=True)
        return
    choices = await _save_path_choices(torrents_link)
    try:
        save_path = choices[int(idx_raw)]
    except (ValueError, IndexError):
        await callback.answer("Директория недоступна.", show_alert=True)
        return
    try:
        await torrents_link.command(
            ACTION_ADD, {"source": item.source, "name": item.name, "save_path": save_path}
        )
    except ServiceUnavailableError:
        await callback.message.edit_text(_unavailable_text(torrents_link))
        await callback.answer()
        return
    except ProtoError as exc:
        await callback.message.edit_text(f"⚠️ Ошибка: {exc.message}")
        await callback.answer()
        return
    await callback.message.edit_text(
        f"✅ <b>{item.name}</b> → <code>{save_path.removeprefix('/mnt/')}</code>"
    )
    await callback.answer()
