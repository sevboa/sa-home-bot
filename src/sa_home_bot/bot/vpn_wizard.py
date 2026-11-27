from __future__ import annotations

import html
import random
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks, vpn_keyboards
from sa_home_bot.bot.ui.keyboards import back_row, button, markup
from sa_home_bot.config import VpnConfig
from sa_home_bot.vpn import protocol as vpn_protocol

PREFIX = "w"

PICK, EXPERT = "p", "x"
INSTALL, FILE, CONNECT, CHECK = "a", "b", "c", "d"
DONE, FAIL, OTHER, HELP = "y", "f", "o", "h"
NOT_CREATED = "n"

INTRO_TEXT = (
    "📶 <b>VPN</b>\n\nПодключим ваш телефон или компьютер?\nПроведу по шагам — займёт пару минут."
)
PICK_TEXT = "Какое у вас устройство?"
CHECK_TEXT = "Включите VPN в Hiddify — «Нажмите для подключения». Подключилось?"
DONE_TEXT = "🎉 Готово! VPN включается в Hiddify — «Нажмите для подключения»."
FAIL_TEXT = "Ничего страшного."
RECHECK_TEXT = "🔄 Проверить ещё раз"
OTHER_TEXT = (
    "Обновите подписку в Hiddify: откройте приложение и нажмите значок обновления на "
    "подключении «VPN». В подписке сразу все страны — Hiddify сам выберет рабочую.\n"
    "Не помогло — позовите на помощь."
)
REFRESH_BUTTON = "🔄 Обновить подписку в Hiddify"
ADD_BUTTON = "➕ Добавить в Hiddify"
LINK_BUTTON = "🔗 Ссылка подписки"
STORE_BUTTONS = {"i": "🍎 App Store", "a": "🤖 Google Play", "c": "💻 hiddify.com"}
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
    name: str

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.name}"


PLATFORMS: dict[str, Platform] = {
    "i": Platform("i", "📱", "iPhone"),
    "a": Platform("a", "🤖", "Android"),
    "c": Platform("c", "💻", "Компьютер"),
}
PICK_BUTTONS = {"i": "🍎 iPhone / iPad / Mac", "a": "🤖 Android", "c": "💻 Компьютер"}


def next_label(platform: Platform, existing: set[str]) -> str:
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


def checking_text(label: str) -> str:
    return f"⏳ Проверяю подключение «{html.escape(label)}»…"


def not_connected_text(label: str) -> str:
    name = html.escape(label)
    return (
        f"Пока не вижу подключения устройства «{name}».\n\n"
        "Проверка видит только VPN, включённый подключением этого устройства:\n"
        f"• в Hiddify — профиль «VPN · {name}».\n\n"
        f"Включите его — в Hiddify «Нажмите для подключения» — и нажмите «{RECHECK_TEXT}». "
        "Не выходит — позовите на помощь."
    )


def connected_text(method: str, server: str) -> str:
    return (
        f"🎉 Готово! Подключено через <b>{html.escape(method)}</b>, сервер "
        f"<b>{html.escape(server)}</b>.\n"
        "Включать VPN — в Hiddify: «Нажмите для подключения»."
    )


def online_connection(answered: list[dict]) -> tuple[str, str] | None:
    best: tuple[str, str, str] | None = None
    for server in answered:
        for conn in server.get("connections") or []:
            if not conn.get("online"):
                continue
            seen = str(conn.get("seen_at") or "")
            if best is None or seen > best[0]:
                name = server.get("label") or server.get("node") or "сервер"
                best = (seen, str(conn.get("transport") or ""), str(name))
    return None if best is None else (best[1], best[2])


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
        back_row(vpn_callbacks.home_cb()),
    )


def page_link_text(label: str, page_url: str) -> str:
    return (
        f"📶 <b>Настройки VPN · {html.escape(label)}</b>\n"
        "Откройте ссылку на устройстве, которое подключаете (телефон или компьютер) — там всё "
        "по шагам: Hiddify и, если нужно, AmneziaVPN, и проверка, что VPN включён.\n"
        f"{html.escape(page_url)}\n"
        "Ссылку можно переслать. Не делитесь ей с чужими: по ней подключается это устройство."
    )


def add_url(page_url: str) -> str:
    return f"{page_url.rstrip('/')}/{vpn_protocol.SUB_BRIDGE_HIDDIFY}"


def platform_of_label(label: str) -> str | None:
    for code, platform in PLATFORMS.items():
        if label.startswith(platform.emoji):
            return code
    return None


def store_url(code: str, cfg: VpnConfig) -> str:
    if code == "i":
        return cfg.hiddify_ios_app_store_url
    if code == "a":
        return cfg.hiddify_google_play_url
    return cfg.hiddify_site_url


def install_row(code: str | None, cfg: VpnConfig) -> list[InlineKeyboardButton]:
    codes = [code] if code in STORE_BUTTONS else ["i", "a"]
    return [InlineKeyboardButton(text=STORE_BUTTONS[c], url=store_url(c, cfg)) for c in codes]


def add_row(page_url: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=ADD_BUTTON, url=add_url(page_url))]


def connect_text(label: str, sub_link: str | None) -> str:
    lines = [
        f"📶 <b>VPN · {html.escape(label)}</b>",
        "",
        "① Поставьте Hiddify — кнопка ниже.",
        f"② Нажмите «{ADD_BUTTON}» и подтвердите добавление. В Hiddify нажмите "
        "«Нажмите для подключения».",
    ]
    if sub_link:
        lines += [
            "③ Не добавилось? Нажмите на ссылку — она скопируется, затем в Hiddify нажмите «+» → "
            "«Добавить из буфера обмена»:",
            f"<code>{html.escape(sub_link)}</code>",
        ]
    lines += ["", "Ссылка личная: не делитесь ею с чужими."]
    return "\n".join(lines)


def sub_link_text(label: str, sub_link: str) -> str:
    return (
        f"🔗 <b>Подписка · {html.escape(label)}</b>\n"
        "Нажмите на ссылку — она скопируется, затем в Hiddify нажмите «+» → "
        "«Добавить из буфера обмена»:\n"
        f"<code>{html.escape(sub_link)}</code>\n"
        "Ссылка личная: не делитесь ею с чужими."
    )


SUB_QR_CAPTION = "QR для другого устройства: в Hiddify нажмите «+» → «Сканировать QR-код»."


def add_keyboard(code: str | None, cfg: VpnConfig, page_url: str) -> InlineKeyboardMarkup:
    return markup(install_row(code, cfg), add_row(page_url))


def sub_link_keyboard(page_url: str) -> InlineKeyboardMarkup:
    return markup(add_row(page_url))


def step_keyboard(
    code: str,
    platform: Platform,
    key: str,
    *,
    page_url: str | None = None,
    cfg: VpnConfig | None = None,
) -> InlineKeyboardMarkup:
    p = platform.code
    stuck_callback = card_cb(FAIL, f"{code}{ctx(p, key)}")
    if code == CHECK:
        return markup([button("✅ Да!", card_cb(DONE)), button("❌ Нет", stuck_callback)])
    rows: list[list[InlineKeyboardButton]] = []
    if code == CONNECT and page_url:
        if cfg is not None:
            rows.append(install_row(p, cfg))
        rows.append(add_row(page_url))
    stuck = button("🙋 Не получается", stuck_callback)
    ready = button("✅ Готово", card_cb(CHECK, ctx(p, key)))
    rows.append([ready, stuck] if not (code == CONNECT and not page_url) else [stuck])
    return markup(*rows)


def not_connected_keyboard(
    platform: str, key: str, *, page_url: str | None = None
) -> InlineKeyboardMarkup:
    rows = [[button(RECHECK_TEXT, card_cb(CHECK, ctx(platform, key)))]]
    if page_url:
        rows.append(add_row(page_url))
    return markup(
        *rows,
        _help_button(CHECK, platform, key),
        back_row(vpn_callbacks.home_cb(), vpn_keyboards.HOME_TEXT),
    )


def fail_keyboard(step: str, platform: str, key: str) -> InlineKeyboardMarkup:
    rows = []
    if step != NOT_CREATED:
        tail = f"{step}{ctx(platform, key)}"
        rows.append([button(REFRESH_BUTTON, card_cb(OTHER, tail))])
    rows.append(_help_button(step, platform, key))
    rows.append(back_row(_step_callback(step, platform, key), BACK_TO_STEP_TEXT))
    return markup(*rows)


def back_to_step_keyboard(
    step: str, platform: str, key: str, *, help_button: bool
) -> InlineKeyboardMarkup:
    rows = []
    if help_button:
        rows.append(_help_button(step, platform, key))
    rows.append(back_row(_step_callback(step, platform, key), BACK_TO_STEP_TEXT))
    return markup(*rows)


def create_failed_keyboard(platform: str) -> InlineKeyboardMarkup:
    return markup(
        [button(vpn_keyboards.RETRY_TEXT, create_cb(platform))],
        _help_button(NOT_CREATED, platform, ""),
        back_row(vpn_callbacks.home_cb()),
    )


def unavailable_keyboard(step: str, platform: str, key: str) -> InlineKeyboardMarkup:
    return markup(
        [button("🔁 Повторить", card_cb(step, ctx(platform, key)))],
        _help_button(step, platform, key),
    )
