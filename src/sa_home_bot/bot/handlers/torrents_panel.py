from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import apps_view, commands, torrents_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.bot.ui.callback_data import int_or
from sa_home_bot.bot.ui.redraw import redraw
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)

router = Router(name="torrents_panel")

ACTION_LIST = "list"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_SPEED_LIMIT = "speed_limit"


async def _fetch(torrents_link: ServiceLink) -> dict | None:
    try:
        return await torrents_link.command(ACTION_LIST, {"with_hash": True})
    except (ServiceUnavailableError, ProtoError):
        return None


def _find(result: dict, thash: str) -> dict | None:
    return next((t for t in result.get("torrents", ()) if t.get("hash") == thash), None)


@router.message(Command(commands.TORRENTS.name))
async def cmd_torrents(message: Message, torrents_link: ServiceLink) -> None:
    result = await _fetch(torrents_link)
    if result is None:
        await message.answer(torrents_view.UNAVAILABLE_TEXT)
        return
    text, keyboard = torrents_view.build_list_view(result, 0)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith(f"{commands.CALLBACK_PREFIX}:t_"))
async def on_torrents_screen(
    callback: CallbackQuery, torrents_link: ServiceLink, apps_link: ServiceLink
) -> None:
    parts = (callback.data or "").split(":")
    code = parts[1] if len(parts) > 1 else ""

    if code == commands.TORRENT_APP_CODE:
        text, _ignored_keyboard = await apps_view.run_app_skill(
            apps_link, None, apps_view.QBITTORRENT_APP_ID
        )
        text, keyboard = torrents_view.build_app_view(text)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.TORRENTS_LIST_CODE:
        offset = int_or(parts[2] if len(parts) > 2 else None)
        result = await _fetch(torrents_link)
        if result is None:
            await redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer()
            return
        text, keyboard = torrents_view.build_list_view(result, offset)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.TORRENT_CARD_CODE:
        thash = parts[2] if len(parts) > 2 else ""
        result = await _fetch(torrents_link)
        if result is None:
            await redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer()
            return
        torrent = _find(result, thash)
        if torrent is None:
            await callback.answer("Раздача пропала из списка — обновите.", show_alert=True)
            return
        text, keyboard = torrents_view.build_card_view(torrent)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.TORRENT_TOGGLE_CODE:
        thash = parts[2] if len(parts) > 2 else ""
        result = await _fetch(torrents_link)
        if result is None:
            await redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer()
            return
        torrent = _find(result, thash)
        if torrent is None:
            await callback.answer("Раздача пропала из списка — обновите.", show_alert=True)
            return
        action = ACTION_RESUME if torrent.get("paused") else ACTION_PAUSE
        try:
            await torrents_link.command(action, {"name": thash})
        except ServiceUnavailableError:
            await callback.answer(torrents_view.UNAVAILABLE_TEXT, show_alert=True)
            return
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        result = await _fetch(torrents_link)
        if result is None:
            await redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer("Готово")
            return
        torrent = _find(result, thash)
        if torrent is None:
            await callback.answer("Готово")
            return
        text, keyboard = torrents_view.build_card_view(torrent)
        await redraw(callback, text, keyboard)
        await callback.answer("Готово")
        return

    if code == commands.TORRENT_SPEED_CODE:
        mbps = int_or(parts[2] if len(parts) > 2 else None)
        offset = int_or(parts[3] if len(parts) > 3 else None)
        try:
            await torrents_link.command(ACTION_SPEED_LIMIT, {"mbps": mbps})
        except ServiceUnavailableError:
            await callback.answer(torrents_view.UNAVAILABLE_TEXT, show_alert=True)
            return
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        result = await _fetch(torrents_link)
        if result is None:
            await redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer("Лимит обновлён")
            return
        text, keyboard = torrents_view.build_list_view(result, offset)
        await redraw(callback, text, keyboard)
        await callback.answer("Лимит обновлён")
        return

    await callback.answer()
