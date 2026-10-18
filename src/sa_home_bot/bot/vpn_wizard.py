"""Пошаговая настройка /vpn (этап 57.3a): тексты, нумерация, кнопки.

Состояние живёт в callback_data (``act:vpn:vpn_card:w<экран><…>``, право
``vpn_card@vpn`` есть у любого гостя с VPN); создаёт устройство отдельная
кнопка ``act:vpn:issue:~w<платформа>`` — под обычным ``issue@vpn``.

Экраны (``w`` + код):
  p — выбор устройства              x — «Я разберусь сам» (старая карточка)
  b/c/d — шаги: файл настроек (если включён) / страница настроек / проверка
  (57.11: установка и подключение живут на странице; код ``a`` остаётся для
  старых кнопок и ведёт на страницу)
  y — «Заработало»                  f/o/h + <шаг><платформа><ключ> — «Не
  получается» / другая страна / позвать на помощь. Шаг ``n`` — устройство не
  удалось создать (ключа нет).
Платформа — одна буква (i/a/c), ключ устройства — ``vpn_devices.device_key``.
"""

from __future__ import annotations

import html
import random
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks, vpn_keyboards
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.vpn import protocol as vpn_protocol

PREFIX = "w"

# Коды экранов.
PICK, EXPERT = "p", "x"
INSTALL, FILE, CONNECT, CHECK = "a", "b", "c", "d"
DONE, FAIL, OTHER, HELP = "y", "f", "o", "h"
NOT_CREATED = "n"  # «шаг» для помощи, когда устройство не удалось создать

INTRO_TEXT = (
    "📶 <b>VPN</b>\n\nПодключим ваш телефон или компьютер?\nПроведу по шагам — займёт пару минут."
)
PICK_TEXT = "Какое у вас устройство?"
CHECK_TEXT = "Нажмите большую круглую кнопку в середине Hiddify.\nЗаработало?"
DONE_TEXT = "🎉 Готово! Теперь VPN включается этой круглой кнопкой."
FAIL_TEXT = "Ничего страшного."
OTHER_TEXT = "В Hiddify нажмите на название подключения и выберите другую страну."
CREATE_FAILED_TEXT = (
    "Не получилось подготовить подключение — серверы сейчас не отвечают.\n"
    "Попробуйте ещё раз чуть позже или позовите на помощь."
)
HELP_SENT_TEXT = "✅ Передал владельцу. Ответ придёт сюда."
HELP_THROTTLED_TEXT = "Уже передал, ждите ответа."
HELP_NOBODY_TEXT = "Сейчас некому передать. Попробуйте написать владельцу напрямую."
UNAVAILABLE_TEXT = (
    "Не получилось подготовить этот шаг — сервер не ответил.\n"
    "Повторите чуть позже или позовите на помощь."
)
BACK_TO_STEP_TEXT = "⬅️ Назад к шагу"


@dataclass(frozen=True)
class Platform:
    code: str
    emoji: str
    name: str  # «iPhone»

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


PLATFORMS: dict[str, Platform] = {
    "i": Platform("i", "📱", "iPhone"),
    "a": Platform("a", "🤖", "Android"),
    "c": Platform("c", "💻", "Компьютер"),
}
PICK_BUTTONS = {"i": "🍎 iPhone", "a": "🤖 Android", "c": "💻 Компьютер"}


def next_label(platform: Platform, existing: set[str]) -> str:
    """«🤖 Peony»: эмодзи платформы + цветок, которого у человека ещё нет (с эмодзи
    или без — старые устройства звались просто «Rose»). Цветы кончились — с номером."""
    taken = {label.split(" ", 1)[-1] for label in existing}
    free = [name for name in vpn_protocol.FLOWER_NAMES if name not in taken]
    if free:
        return f"{platform.emoji} {random.choice(free)}"
    n = 2
    while f"{platform.emoji} {vpn_protocol.FLOWER_NAMES[0]} {n}" in existing:
        n += 1
    return f"{platform.emoji} {vpn_protocol.FLOWER_NAMES[0]} {n}"


def stuck_text(code: str, platform: Platform | None) -> str:
    where = f" ({platform.name})" if platform else ""
    if code == NOT_CREATED:
        return f"не удалось подготовить подключение{where}"
    if code == CHECK:
        return f"застрял(а) на проверке: после подключения не заработало{where}"
    step = "1 из 1" if code == CONNECT else code
    return f"застрял(а) на шаге {step}{where}"


def card_cb(code: str, rest: str = "") -> str:
    return vpn_callbacks.card_cb(f"{PREFIX}{code}{rest}")


def ctx(platform: str, key: str = "") -> str:
    return f"{platform}{key}"


def create_cb(platform: str) -> str:
    return vpn_callbacks.expert_cb(vpn_protocol.ACTION_ISSUE, PREFIX, platform)


def wizard_button() -> InlineKeyboardButton:
    return button("📱 Подключить по шагам", card_cb(PICK))


def parse(value: str) -> tuple[str, str, str]:
    code, rest = value[1:2], value[2:]
    if code in (FAIL, OTHER, HELP):
        return code, rest[:1], rest[1:]
    return code, "", rest


def _step_callback(step: str, platform: str, key: str) -> str:
    return card_cb(PICK) if step == NOT_CREATED else card_cb(step, ctx(platform, key))


def _help_button(step: str, platform: str, key: str) -> list[InlineKeyboardButton]:
    return [button(vpn_keyboards.CALL_FOR_HELP_TEXT, card_cb(HELP, f"{step}{ctx(platform, key)}"))]


def intro_keyboard() -> InlineKeyboardMarkup:
    return markup([wizard_button()], [button("⚙️ Я разберусь сам", card_cb(EXPERT))])


def pick_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [button(text, create_cb(code)) for code, text in PICK_BUTTONS.items()],
        vpn_keyboards.back_row(vpn_callbacks.home_cb()),
    )


def page_link_text(label: str, page_url: str) -> str:
    return (
        f"📶 <b>Настройки VPN · {html.escape(label)}</b>\n"
        "Откройте ссылку на устройстве, которое подключаете (телефон или компьютер) — там всё "
        "по шагам: AmneziaVPN, Hiddify и проверка, что VPN включён.\n"
        f"{html.escape(page_url)}\n"
        "Ссылку можно переслать. Не делитесь ей с чужими: по ней подключается это устройство."
    )


def step_keyboard(
    code: str, platform: Platform, key: str, *, page_url: str | None = None
) -> InlineKeyboardMarkup:
    p = platform.code
    stuck_callback = card_cb(FAIL, f"{code}{ctx(p, key)}")
    if code == CHECK:
        return markup([button("✅ Да!", card_cb(DONE)), button("❌ Нет", stuck_callback)])
    rows: list[list[InlineKeyboardButton]] = []
    if code == CONNECT and page_url:
        rows.append([InlineKeyboardButton(text="📶 Открыть настройки", url=page_url)])
    stuck = button("🙋 Не получается", stuck_callback)
    ready = button("✅ Готово", card_cb(CHECK, ctx(p, key)))
    rows.append([ready, stuck] if not (code == CONNECT and not page_url) else [stuck])
    return markup(*rows)


def fail_keyboard(step: str, platform: str, key: str) -> InlineKeyboardMarkup:
    rows = []
    if step != NOT_CREATED:
        tail = f"{step}{ctx(platform, key)}"
        rows.append([button("🔁 Попробовать другую страну", card_cb(OTHER, tail))])
    rows.append(_help_button(step, platform, key))
    rows.append(vpn_keyboards.back_row(_step_callback(step, platform, key), BACK_TO_STEP_TEXT))
    return markup(*rows)


def back_to_step_keyboard(
    step: str, platform: str, key: str, *, help_button: bool
) -> InlineKeyboardMarkup:
    rows = []
    if help_button:
        rows.append(_help_button(step, platform, key))
    rows.append(vpn_keyboards.back_row(_step_callback(step, platform, key), BACK_TO_STEP_TEXT))
    return markup(*rows)


def create_failed_keyboard(platform: str) -> InlineKeyboardMarkup:
    return markup(
        [button(vpn_keyboards.RETRY_TEXT, create_cb(platform))],
        _help_button(NOT_CREATED, platform, ""),
        vpn_keyboards.back_row(vpn_callbacks.home_cb()),
    )


def unavailable_keyboard(step: str, platform: str, key: str) -> InlineKeyboardMarkup:
    return markup(
        [button("🔁 Повторить", card_cb(step, ctx(platform, key)))],
        _help_button(step, platform, key),
    )
