"""Пошаговая настройка /vpn (этап 57.3a): тексты, нумерация, кнопки.

Состояние живёт в callback_data (``act:vpn:vpn_card:w<экран><…>``, право
``vpn_card@vpn`` есть у любого гостя с VPN); создаёт устройство отдельная
кнопка ``act:vpn:issue:~w<платформа>`` — под обычным ``issue@vpn``.

Экраны (``w`` + код):
  p — выбор устройства              x — «Я разберусь сам» (старая карточка)
  a/b/c/d — шаги: установка / файл настроек / подключение / проверка
  y — «Заработало»                  f/o/h + <шаг><платформа><ключ> — «Не
  получается» / другая страна / позвать на помощь. Шаг ``n`` — устройство не
  удалось создать (ключа нет).
Платформа — одна буква (i/a/c), ключ устройства — ``vpn_devices.device_key``.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.config import VpnConfig
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME
CARD_ACTION = "vpn_card"
PREFIX = "w"

# Коды экранов.
PICK, EXPERT = "p", "x"
INSTALL, FILE, CONNECT, CHECK = "a", "b", "c", "d"
DONE, FAIL, OTHER, HELP = "y", "f", "o", "h"
NOT_CREATED = "n"  # «шаг» для помощи, когда устройство не удалось создать
STEP_ORDER = (INSTALL, FILE, CONNECT)

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
FILE_CAPTION = "Настройки Hiddify. Нажмите на файл и выберите «Hiddify»."


@dataclass(frozen=True)
class Platform:
    code: str
    emoji: str
    name: str  # «iPhone»
    store_button: str

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


PLATFORMS: dict[str, Platform] = {
    "i": Platform("i", "📱", "iPhone", "⬇️ Открыть App Store"),
    "a": Platform("a", "🤖", "Android", "⬇️ Открыть Google Play"),
    "c": Platform("c", "💻", "Компьютер", "⬇️ Открыть сайт Hiddify"),
}
PICK_BUTTONS = {"i": "🍎 iPhone", "a": "🤖 Android", "c": "💻 Компьютер"}


def store_url(platform: Platform, cfg: VpnConfig) -> str:
    return {
        "i": cfg.hiddify_ios_app_store_url,
        "a": cfg.hiddify_google_play_url,
    }.get(platform.code, cfg.hiddify_site_url)


def next_label(platform: Platform, existing: set[str]) -> str:
    """«📱 iPhone», при повторе «📱 iPhone 2», «📱 iPhone 3» …"""
    if platform.label not in existing:
        return platform.label
    n = 2
    while f"{platform.label} {n}" in existing:
        n += 1
    return f"{platform.label} {n}"


def needs_settings_file(cfg: VpnConfig, devices: list[vd.Device], key: str) -> bool:
    """Шаг «файл настроек» нужен, если включён флагом и ни одно ДРУГОЕ устройство
    человека ещё не выходило на связь по VLESS (Hiddify не настроен)."""
    if not cfg.wizard_settings_file:
        return False
    return not any(
        conn.transport == vd.REALITY and conn.last_handshake_at
        for dev in devices
        if dev.key != key
        for conn in dev.issued
    )


def steps(needs_file: bool) -> list[str]:
    return [s for s in STEP_ORDER if s != FILE or needs_file]


def step_title(code: str, needs_file: bool) -> str | None:
    order = steps(needs_file)
    if code not in order:
        return None
    return f"Шаг {order.index(code) + 1} из {len(order)}"


def next_step(code: str, needs_file: bool) -> str:
    order = steps(needs_file)
    return order[order.index(code) + 1] if order.index(code) + 1 < len(order) else CHECK


def stuck_text(code: str, platform: Platform | None, needs_file: bool) -> str:
    """Причина для владельца: «застрял(а) на шаге 2 из 3 (iPhone)»."""
    where = f" ({platform.name})" if platform else ""
    if code == NOT_CREATED:
        return f"не удалось подготовить подключение{where}"
    if code == CHECK:
        return f"застрял(а) на проверке: после подключения не заработало{where}"
    title = step_title(code, needs_file)
    return f"застрял(а) на шаге {title.removeprefix('Шаг ') if title else code}{where}"


# --- callback_data ---------------------------------------------------------


def card_cb(code: str, rest: str = "") -> str:
    return commands.action_callback(CARD_ACTION, f"{PREFIX}{code}{rest}", service=SERVICE)


def ctx(platform: str, key: str = "") -> str:
    return f"{platform}{key}"


def create_cb(platform: str) -> str:
    return commands.action_callback(
        vpn_protocol.ACTION_ISSUE, f"~{PREFIX}{platform}", service=SERVICE
    )


def home_cb() -> str:
    return commands.action_callback(CARD_ACTION, service=SERVICE)


def wizard_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="📱 Подключить по шагам", callback_data=card_cb(PICK))


def parse(value: str) -> tuple[str, str, str]:
    """``w<код><остаток>`` → (код, шаг-для-f/o/h или '', остаток). Для f/o/h
    остаток = ``<платформа><ключ>``, шаг вынут отдельно."""
    code, rest = value[1:2], value[2:]
    if code in (FAIL, OTHER, HELP):
        return code, rest[:1], rest[1:]
    return code, "", rest


# --- клавиатуры ------------------------------------------------------------


def intro_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [wizard_button()],
            [InlineKeyboardButton(text="⚙️ Я разберусь сам", callback_data=card_cb(EXPERT))],
        ]
    )


def pick_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=create_cb(code))
                for code, text in PICK_BUTTONS.items()
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=home_cb())],
        ]
    )


def step_keyboard(
    code: str,
    platform: Platform,
    key: str,
    cfg: VpnConfig,
    *,
    needs_file: bool,
    page_url: str | None = None,
) -> InlineKeyboardMarkup:
    p = platform.code
    stuck = InlineKeyboardButton(
        text="🙋 Не получается", callback_data=card_cb(FAIL, f"{code}{ctx(p, key)}")
    )
    rows: list[list[InlineKeyboardButton]] = []
    if code == CHECK:
        rows.append(
            [
                InlineKeyboardButton(text="✅ Да!", callback_data=card_cb(DONE)),
                InlineKeyboardButton(
                    text="❌ Нет", callback_data=card_cb(FAIL, f"{code}{ctx(p, key)}")
                ),
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if code == INSTALL:
        rows.append(
            [InlineKeyboardButton(text=platform.store_button, url=store_url(platform, cfg))]
        )
    if code == CONNECT and page_url:
        rows.append([InlineKeyboardButton(text="🔌 Подключить", url=page_url)])
    ready = InlineKeyboardButton(
        text="✅ Готово", callback_data=card_cb(next_step(code, needs_file), ctx(p, key))
    )
    rows.append([ready, stuck] if not (code == CONNECT and not page_url) else [stuck])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fail_keyboard(
    step: str, platform: str, key: str, *, can_help: bool = True
) -> InlineKeyboardMarkup:
    tail = f"{step}{ctx(platform, key)}"
    back = card_cb(PICK) if step == NOT_CREATED else card_cb(step, ctx(platform, key))
    rows = []
    if step != NOT_CREATED:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔁 Попробовать другую страну", callback_data=card_cb(OTHER, tail)
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="🙋 Позвать на помощь", callback_data=card_cb(HELP, tail))]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад к шагу", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_to_step_keyboard(
    step: str, platform: str, key: str, *, help_button: bool
) -> InlineKeyboardMarkup:
    rows = []
    if help_button:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🙋 Позвать на помощь",
                    callback_data=card_cb(HELP, f"{step}{ctx(platform, key)}"),
                )
            ]
        )
    back = card_cb(PICK) if step == NOT_CREATED else card_cb(step, ctx(platform, key))
    rows.append([InlineKeyboardButton(text="⬅️ Назад к шагу", callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def done_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📶 На главную", callback_data=home_cb())]]
    )


def create_failed_keyboard(platform: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Попробовать ещё раз", callback_data=create_cb(platform)
                )
            ],
            [
                InlineKeyboardButton(
                    text="🙋 Позвать на помощь",
                    callback_data=card_cb(HELP, f"{NOT_CREATED}{platform}"),
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=home_cb())],
        ]
    )


def unavailable_keyboard(step: str, platform: str, key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Повторить", callback_data=card_cb(step, ctx(platform, key))
                )
            ],
            [
                InlineKeyboardButton(
                    text="🙋 Позвать на помощь",
                    callback_data=card_cb(HELP, f"{step}{ctx(platform, key)}"),
                )
            ],
        ]
    )
