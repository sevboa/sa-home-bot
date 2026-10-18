from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks
from sa_home_bot.bot.ui.keyboards import button, single_button
from sa_home_bot.vpn import protocol as vpn_protocol

BACK_TEXT = "⬅️ Назад"
HOME_TEXT = "📶 На главную"
CALL_FOR_HELP_TEXT = "🙋 Позвать на помощь"
RETRY_TEXT = "🔁 Попробовать ещё раз"
HELP_TEXT = "❓ Помощь"
NEW_DEVICE_TEXT = "➕ Новое устройство"
GRANT_EXTRA_TEXT = "➕ 100 ГБ"


def back_row(callback_data: str, text: str = BACK_TEXT) -> list[InlineKeyboardButton]:
    return [button(text, callback_data)]


def done_keyboard(home_cb: str | None = None) -> InlineKeyboardMarkup:
    return single_button(HOME_TEXT, home_cb or vpn_callbacks.home_cb())


def help_button() -> InlineKeyboardButton:
    return button(HELP_TEXT, vpn_callbacks.vpn_callback(vpn_callbacks.ACTION_APK))


def grant_extra_button(node: str | None, suffix: str = "") -> InlineKeyboardButton:
    return button(
        f"{GRANT_EXTRA_TEXT}{suffix}",
        vpn_callbacks.vpn_callback(vpn_protocol.ACTION_GRANT_EXTRA, node=node),
    )


def suffix_after_space(text: str) -> str:
    return f" {text}" if text else ""
