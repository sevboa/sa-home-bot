"""«❓ Помощь» в /vpn (этап 57.6): вопросы кнопками, ответ — 2–3 строки.

Чистый модуль (тексты и клавиатуры, без сети). Все экраны живут под действием
``apk`` (право ``apk@vpn``, оно же у кнопки «❓ Помощь»): значение пусто — список
вопросов, ``q<код>`` — ответ. Ответ редактирует то же сообщение; внизу всегда
«⬅️ К вопросам». Те же ответы (bot/vpn_facts.py) лежат в описании ``tool_vpn`` (57.8).
Ссылок на AmneziaWG в магазинах нет нигде — только AmneziaVPN, а AmneziaWG
запасным путём (.apk).
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, vpn_facts, vpn_report, vpn_settings, vpn_wizard
from sa_home_bot.config import VpnConfig
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME
ACTION = "apk"
HOME_ACTION = "vpn_card"

Q_PREFIX = "q"
Q_HIDDIFY, Q_AWG, Q_TROUBLE, Q_DEVICE = (
    vpn_facts.Q_HIDDIFY,
    vpn_facts.Q_AWG,
    vpn_facts.Q_TROUBLE,
    vpn_facts.Q_DEVICE,
)
Q_STORE, Q_ASK = "s", "x"

QUESTIONS: tuple[tuple[str, str], ...] = (
    (Q_HIDDIFY, "Что такое Hiddify?"),
    (Q_AWG, "Чем отличается AmneziaWG?"),
    (Q_TROUBLE, "Не подключается"),
    (Q_DEVICE, "Как подключить ещё одно устройство"),
    (Q_STORE, "Магазин недоступен?"),
)
ASK_BUTTON = "🤵 Спросить Альфреда"
BACK_BUTTON = "⬅️ К вопросам"

LIST_TEXT = "❓ <b>Помощь</b>\n\nВыберите вопрос."

ANSWERS = {
    **vpn_facts.ANSWERS,
    Q_ASK: "Напишите Альфреду вопрос — например: /alfred не подключается VPN на iPhone",
}
HIDDIFY_NO_STORE = "Hiddify можно скачать и с сайта — кнопки ниже."


def list_cb() -> str:
    return commands.action_callback(ACTION, service=SERVICE)


def question_cb(code: str) -> str:
    return commands.action_callback(ACTION, f"{Q_PREFIX}{code}", service=SERVICE)


def parse_question(value: str | None) -> str | None:
    """``q<код>`` → код известного ответа, иначе None."""
    if (
        value
        and value.startswith(Q_PREFIX)
        and value[len(Q_PREFIX) :]
        in {
            *ANSWERS,
            Q_STORE,
        }
    ):
        return value[len(Q_PREFIX) :]
    return None


def list_keyboard(*, home_cb: str, report: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=text, callback_data=question_cb(code))]
        for code, text in QUESTIONS
    ]
    last = [InlineKeyboardButton(text=ASK_BUTTON, callback_data=question_cb(Q_ASK))]
    if report:
        last.append(vpn_report.report_button(faq=True))
    rows.append(last)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=home_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hiddify_buttons(cfg: VpnConfig) -> list[list[InlineKeyboardButton]]:
    rows = [
        [
            InlineKeyboardButton(text="🍎 App Store", url=cfg.hiddify_ios_app_store_url),
            InlineKeyboardButton(text="🤖 Google Play", url=cfg.hiddify_google_play_url),
        ]
    ]
    site = [
        InlineKeyboardButton(text=text, url=url)
        for text, url in (
            ("🌐 Сайт", cfg.hiddify_site_url),
            ("🖥 Компьютер / APK", cfg.hiddify_releases_url),
        )
        if url
    ]
    if site:
        rows.append(site)
    return rows


def _amnezia_buttons(cfg: VpnConfig) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(text="App Store", url=cfg.amneziavpn_ios_app_store_url),
            InlineKeyboardButton(text="Google Play", url=cfg.amneziavpn_google_play_url),
            InlineKeyboardButton(text="Сайт", url=cfg.official_download_url),
        ]
    ]


def answer_text(code: str) -> str:
    if code == Q_STORE:
        return f"{vpn_settings.NO_STORE_TEXT}\n\n{HIDDIFY_NO_STORE}"
    return ANSWERS[code]


def answer_keyboard(code: str, cfg: VpnConfig, *, can_wizard: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if code == Q_HIDDIFY:
        rows += _hiddify_buttons(cfg)
    elif code == Q_AWG:
        rows += _amnezia_buttons(cfg)
    elif code == Q_TROUBLE:
        rows.append([InlineKeyboardButton(text=ASK_BUTTON, callback_data=question_cb(Q_ASK))])
    elif code == Q_DEVICE and can_wizard:
        rows.append([vpn_wizard.wizard_button()])
    elif code == Q_STORE:
        rows += vpn_settings.no_store_keyboard().inline_keyboard
        rows += _hiddify_buttons(cfg)[1:]
    rows.append([InlineKeyboardButton(text=BACK_BUTTON, callback_data=list_cb())])
    return InlineKeyboardMarkup(inline_keyboard=rows)
