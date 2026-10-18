from __future__ import annotations

import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks
from sa_home_bot.bot.ui.keyboards import back_row, button, markup, single_button
from sa_home_bot.bot.vpn_devices import Country
from sa_home_bot.vpn import protocol as vpn_protocol

ACTION = vpn_protocol.ACTION_PROXY_LINK
VALUE_QR = "qr"
VALUE_SECRET = "d"
OPEN_BUTTON_TEXT = "✈️ Прокси Telegram"
ONLY_PROXY_CARD = (
    "📶 <b>VPN</b>\n\n"
    "✈️ Прокси Telegram вам открыт.\n"
    "VPN-доступ к локациям пока не выдан — попросите владельца, если он нужен."
)

TEXT = (
    "✈️ <b>Прокси Telegram</b>\n\n"
    "Telegram без VPN. Нажмите кнопку — Telegram сам предложит подключить."
)


def country_of(result: dict) -> Country:
    node = str(result.get("node") or "")
    return Country(node, str(result.get("label") or node))


def _link(result: dict) -> str:
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
        button(
            "📷 QR" if len(usable) == 1 else f"📷 QR {country_of(r).short}",
            vpn_callbacks.vpn_callback(ACTION, VALUE_QR, r.get("node")),
        )
        for r in usable
        if r.get("qr_png_b64") and r.get("node")
    ]
    if qr:
        rows.append(qr)
    if can_secret:
        rows.append([button("⚙️ Секрет и порт", vpn_callbacks.vpn_callback(ACTION, VALUE_SECRET))])
    rows.append(back_row(home_cb))
    return markup(*rows)


def proxy_text(result: dict, *, admin: bool) -> str:
    label = result.get("label")
    heading = "✈️ <b>Прокси Telegram (mtg)</b>"
    if label:
        heading += f" · {html.escape(str(label))}"
    heading += " — общая ссылка, один секрет на всех.\n"
    text = (
        heading + f"Ссылка: {html.escape(result['tg_link'])}\n"
        f"t.me: {html.escape(result['t_me_link'])}\n\n"
        f"Сервер: <code>{html.escape(str(result['host']))}</code>\n"
        f"Порт: <code>{result['port']}</code>\n"
        f"Секрет: <code>{html.escape(result['secret'])}</code>"
    )
    if admin:
        text += (
            "\n\n🧦 SOCKS5 для ботов (доступен только внутри tailnet):\n"
            f"<code>{html.escape(str(result['socks_host']))}:{result['socks_port']}</code>"
        )
    return text


def only_proxy_keyboard() -> InlineKeyboardMarkup:
    return single_button(OPEN_BUTTON_TEXT, vpn_callbacks.vpn_callback(ACTION))


def rotate_keyboard(
    node_id: str | None = None, *, can_rotate: bool = True
) -> InlineKeyboardMarkup | None:
    if not can_rotate:
        return None
    return single_button(
        "🔁 Сменить секрет (старая ссылка перестанет работать)",
        vpn_callbacks.vpn_callback(vpn_protocol.ACTION_PROXY_ROTATE_SECRET, node=node_id),
    )
