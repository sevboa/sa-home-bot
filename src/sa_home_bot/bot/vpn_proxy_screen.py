"""Экран «✈️ Прокси Telegram» (этап 57.6): кнопки по странам вместо ссылок.

Действие ``proxy_link`` (право ``proxy_link@vpn``): значение пусто — экран,
``qr`` + node_id — QR этой страны, ``d`` — «⚙️ Секрет и порт» (подробности
показываются только тем, у кого есть ``proxy_rotate_secret@vpn``; проверка в
обработчике, кнопка рисуется только им). Нода, не ответившая на опрос, в
``results`` не попала — её кнопок нет.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot.vpn_devices import Country
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME
ACTION = vpn_protocol.ACTION_PROXY_LINK
VALUE_QR = "qr"
VALUE_SECRET = "d"

TEXT = (
    "✈️ <b>Прокси Telegram</b>\n\n"
    "Telegram без VPN. Нажмите кнопку — Telegram сам предложит подключить."
)


def country_of(result: dict) -> Country:
    node = str(result.get("node") or "")
    return Country(node, str(result.get("label") or node))


def _link(result: dict) -> str:
    # Кнопке-ссылке годится https; tg:// — запасной вариант, если нода не прислала.
    return str(result.get("t_me_link") or result.get("tg_link") or "")


def keyboard(results: list[dict], *, can_secret: bool, home_cb: str) -> InlineKeyboardMarkup:
    usable = [r for r in results if _link(r)]
    rows: list[list[InlineKeyboardButton]] = []
    connect = [
        InlineKeyboardButton(text=f"{country_of(r).short} Подключить", url=_link(r)) for r in usable
    ]
    if connect:
        rows.append(connect)
    qr = [
        InlineKeyboardButton(
            text="📷 QR" if len(usable) == 1 else f"📷 QR {country_of(r).short}",
            callback_data=commands.action_callback(
                ACTION, VALUE_QR, service=SERVICE, node_id=r.get("node")
            ),
        )
        for r in usable
        if r.get("qr_png_b64") and r.get("node")
    ]
    if qr:
        rows.append(qr)
    if can_secret:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚙️ Секрет и порт",
                    callback_data=commands.action_callback(ACTION, VALUE_SECRET, service=SERVICE),
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=home_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
