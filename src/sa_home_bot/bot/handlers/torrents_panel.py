"""/torrents — панель закачек qBittorrent, иерархия «t_*» в одном сообщении
(список → карточка раздачи), по образцу bot/handlers/swarm_panel.py.

Служба torrents живёт на одной ноде (как vpn), поэтому, в отличие от
swarm_panel/node_view, здесь не нужна dst-адресация — используется тот же
``torrents_link: ServiceLink``, что уже инжектится в bot/handlers/torrents.py
для приёма .torrent-файлов/magnet-ссылок.

Хэш раздачи запрашивается у службы явным флагом ``with_hash`` (не через
объявленный ``ActionParam`` действия ``list`` — см. докстринг
torrents/service.py::_list_sync): нужен только этому экрану бота как
стабильный идентификатор для callback_data, модели он не виден. Право на всю
панель — одно, `list@torrents` (commands.TORRENTS), как у /guests и /swarm:
CallbackAuthorizationMiddleware уже проверила его до вызова этих хендлеров.

Кнопка «🧲 qBittorrent» (``t_app``) — карточка службы apps того же
приложения (юнит qbittorrent-nox, состояние + ссылка на веб-морду), но без
кнопок управления юнитом: это единственный вход к ней (голая команда
/qbittorrent и пункт меню убраны, см. apps_view.HIDDEN_MENU_APP_IDS), и
отдельного права apps `qbittorrent@apps` здесь не спрашивают — кто открыл
/torrents, тому естественно видеть карточку и самого клиента.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import apps_view, commands, torrents_view
from sa_home_bot.bot.service_link import ServiceLink, ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)

router = Router(name="torrents_panel")

ACTION_LIST = "list"
ACTION_PAUSE = "pause"
ACTION_RESUME = "resume"
ACTION_SPEED_LIMIT = "speed_limit"


def _parse_int(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


async def _fetch(torrents_link: ServiceLink) -> dict | None:
    try:
        return await torrents_link.command(ACTION_LIST, {"with_hash": True})
    except (ServiceUnavailableError, ProtoError):
        return None


async def _redraw(callback: CallbackQuery, text: str, keyboard) -> None:
    if callback.message is None:
        return
    with contextlib.suppress(TelegramBadRequest):
        await callback.message.edit_text(text, reply_markup=keyboard)


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
        # subscription=None — карточка apps/qbittorrent открывается без
        # кнопок управления (build_app_view их не строит вовсе), а run_app_skill
        # использует subscription только для них; своя клавиатура строится
        # ниже независимо от того, что вернул бы run_app_skill.
        text, _ignored_keyboard = await apps_view.run_app_skill(
            apps_link, None, apps_view.QBITTORRENT_APP_ID
        )
        text, keyboard = torrents_view.build_app_view(text)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.TORRENTS_LIST_CODE:
        offset = _parse_int(parts[2] if len(parts) > 2 else None)
        result = await _fetch(torrents_link)
        if result is None:
            await _redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer()
            return
        text, keyboard = torrents_view.build_list_view(result, offset)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.TORRENT_CARD_CODE:
        thash = parts[2] if len(parts) > 2 else ""
        result = await _fetch(torrents_link)
        if result is None:
            await _redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer()
            return
        torrent = _find(result, thash)
        if torrent is None:
            await callback.answer("Раздача пропала из списка — обновите.", show_alert=True)
            return
        text, keyboard = torrents_view.build_card_view(torrent)
        await _redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.TORRENT_TOGGLE_CODE:
        thash = parts[2] if len(parts) > 2 else ""
        result = await _fetch(torrents_link)
        if result is None:
            await _redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer()
            return
        torrent = _find(result, thash)
        if torrent is None:
            await callback.answer("Раздача пропала из списка — обновите.", show_alert=True)
            return
        action = ACTION_RESUME if torrent.get("paused") else ACTION_PAUSE
        try:
            # _select в службе понимает точный хэш как есть (см.
            # torrents/service.py::_select) — однозначная адресация без
            # риска попасть по подстроке чужого имени.
            await torrents_link.command(action, {"name": thash})
        except ServiceUnavailableError:
            await callback.answer(torrents_view.UNAVAILABLE_TEXT, show_alert=True)
            return
        except ProtoError as exc:
            await callback.answer(f"⚠️ {exc.message}", show_alert=True)
            return
        result = await _fetch(torrents_link)
        if result is None:
            await _redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer("Готово")
            return
        torrent = _find(result, thash)
        if torrent is None:
            await callback.answer("Готово")
            return
        text, keyboard = torrents_view.build_card_view(torrent)
        await _redraw(callback, text, keyboard)
        await callback.answer("Готово")
        return

    if code == commands.TORRENT_SPEED_CODE:
        mbps = _parse_int(parts[2] if len(parts) > 2 else None)
        offset = _parse_int(parts[3] if len(parts) > 3 else None)
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
            await _redraw(callback, torrents_view.UNAVAILABLE_TEXT, None)
            await callback.answer("Лимит обновлён")
            return
        text, keyboard = torrents_view.build_list_view(result, offset)
        await _redraw(callback, text, keyboard)
        await callback.answer("Лимит обновлён")
        return

    await callback.answer()
