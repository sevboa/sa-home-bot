"""Выдача настроек в экспертном режиме /vpn (этап 57.4): тексты и кнопки.

Экраны (``vpn_card:<код><ключ устройства>``, право ``vpn_card@vpn``):
  g — «Получить настройки»: чем подключаться (VLESS · Hiddify / AmneziaWG)
  a — AmneziaWG: выбор страны
  n — «➕ Новое устройство»: выбор платформы (имя — как в мастере)
  r — «🔄 Перевыпустить ключи» (57.5): галочки по выданным подключениям; значение
      ``r<ключ>[-<подпись списка>-<маска>]``, маска — hex, бит i = i-й выданный
      транспорт в стабильном порядке (страны как у серверов, VLESS перед AmneziaWG);
      подпись (4 hex) — хэш этого списка: изменился между нажатиями — выбор сброшен

Действия, которые могут выпустить ключ, идут под ``issue@vpn`` / ``reissue@vpn``
(``issue:~<вид><ключ>[:<нода>]``): v — VLESS · Hiddify (довыпускает недостающие
страны), s — файл настроек ещё раз, g — AmneziaWG в стране (``reissue:~g…`` —
после предупреждения «новый заменит старый»), n<платформа> — новое устройство.

Модуль чистый (без сети): логику и отправку ведёт bot/handlers/vpn.py.
"""

from __future__ import annotations

import hashlib
import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_wizard as wizard
from sa_home_bot.config import VpnConfig
from sa_home_bot.domain import vpn_check
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_protocol.SERVICE_NAME
CARD_ACTION = "vpn_card"
EXPERT_PREFIX = "~"

# Экраны ``vpn_card``.
SCREEN_PICK, SCREEN_AWG, SCREEN_NEW = "g", "a", "n"
# Виды действий ``issue:~<вид>…`` / ``reissue:~<вид>…``.
KIND_VLESS, KIND_FILE, KIND_AWG, KIND_NEW = "v", "s", "g", "n"
# Перевыпуск по выбранным: ``reissue:~m<ключ>-<подпись>-<маска>``.
SCREEN_REISSUE, KIND_MULTI = "r", "m"

APK_NO_STORE = "nostore"
MAY_NOT_WORK = "сейчас может не работать"

NO_STORE_TEXT = (
    "Можно временно поставить приложение AmneziaWG файлом — лучше, чем ничего. "
    "Подключитесь через него, затем скачайте настоящий AmneziaVPN и перенесите туда настройки."
)
UNAVAILABLE_TEXT = "Не получилось подготовить настройки — сервер не ответил.\nПовторите чуть позже."
NO_AWG_TEXT = "AmneziaWG сейчас не выдаётся ни в одной стране."
VLESS_FILE_CAPTION = "Настройки Hiddify. Нажмите на файл и выберите «Hiddify»."


def card_cb(screen: str, key: str = "") -> str:
    return commands.action_callback(CARD_ACTION, f"{screen}{key}", service=SERVICE)


def action_cb(action: str, kind: str, key: str = "", node: str | None = None) -> str:
    return commands.action_callback(
        action, f"{EXPERT_PREFIX}{kind}{key}", node_id=node, service=SERVICE
    )


def _device_cb(key: str) -> str:
    return card_cb("d", key)


# --- выбор технологии ------------------------------------------------------


def pick_text(label: str) -> str:
    return f"📥 <b>{html.escape(label)}</b> — чем подключаться?"


def pick_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🛡 {vd.TRANSPORT_NAME[vd.REALITY]}",
                    callback_data=action_cb(vpn_protocol.ACTION_ISSUE, KIND_VLESS, key),
                )
            ],
            [InlineKeyboardButton(text="⚡ AmneziaWG", callback_data=card_cb(SCREEN_AWG, key))],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=_device_cb(key))],
        ]
    )


# --- VLESS · Hiddify -------------------------------------------------------


def had_vless_handshake(device: vd.Device) -> bool:
    """Хоть одно VLESS-подключение устройства уже выходило на связь."""
    return any(c.transport == vd.REALITY and c.last_handshake_at for c in device.issued)


def vless_text(
    label: str,
    links: list[tuple[str, str]],
    *,
    has_page: bool,
    file_sent: bool,
    added: list[str],
    reissued: bool = False,
) -> str:
    """``links`` — (название страны, vless://-ссылка); ``added`` — страны,
    довыпущенные только что."""
    lines = [f"🛡 <b>{html.escape(label)}</b> · {vd.TRANSPORT_NAME[vd.REALITY]}", ""]
    if file_sent and has_page:
        lines += [
            "1. Нажмите на файл выше и выберите «Hiddify» — это нужно один раз.",
            "2. Нажмите «🔌 Подключить» — откроется Hiddify, нажмите там «Добавить».",
        ]
    elif file_sent:
        lines.append("Нажмите на файл выше и выберите «Hiddify» — это нужно один раз.")
    elif has_page:
        lines.append("Нажмите «🔌 Подключить» — откроется Hiddify, нажмите там «Добавить».")
    if added:
        lines += ["", "➕ Добавлено: " + ", ".join(html.escape(a) for a in added) + "."]
    if reissued:
        lines += [
            "",
            "Hiddify обновит подключение сам; если нет — нажмите «🔌 Подключить» ещё раз.",
        ]
    lines += [
        "",
        "Если кнопка не сработала: нажмите на ссылку — она скопируется, затем в Hiddify "
        "нажмите «+» → «Из буфера обмена». Каждая страна — отдельная ссылка.",
    ]
    for country, url in links:
        lines += ["", html.escape(country), f"<code>{html.escape(url)}</code>"]
    return "\n".join(lines)


def vless_keyboard(
    *,
    page_url: str | None,
    file_again_key: str | None,
    qr_buttons: list[tuple[str, str]],
) -> InlineKeyboardMarkup:
    """``file_again_key`` — ключ устройства для «⚙️ Файл настроек ещё раз» (None —
    кнопки нет); ``qr_buttons`` — (подпись, callback) по стране."""
    rows: list[list[InlineKeyboardButton]] = []
    if page_url:
        rows.append([InlineKeyboardButton(text="🔌 Подключить", url=page_url)])
    if file_again_key:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚙️ Файл настроек ещё раз",
                    callback_data=action_cb(vpn_protocol.ACTION_ISSUE, KIND_FILE, file_again_key),
                )
            ]
        )
    if qr_buttons:
        rows.append(
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in qr_buttons]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def qr_button_text(country: vd.Country, multi: bool) -> str:
    return f"📷 QR {country.short}" if multi else "📷 QR"


# --- AmneziaWG -------------------------------------------------------------


def awg_pick_text(label: str) -> str:
    return f"⚡ <b>{html.escape(label)}</b> · AmneziaWG — в какой стране?"


def awg_country_button_text(server: dict) -> str:
    name = server.get("label") or server.get("node") or "?"
    health = vd.transport_health(server, vd.AWG)
    if health is not None and health != vpn_check.OK:
        return f"{name} — {MAY_NOT_WORK}"
    return name


def awg_pick_keyboard(key: str, servers: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=awg_country_button_text(server),
                callback_data=action_cb(vpn_protocol.ACTION_ISSUE, KIND_AWG, key, server["node"]),
            )
        ]
        for server in servers
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=card_cb(SCREEN_PICK, key))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def awg_replace_text(label: str, country: vd.Country) -> str:
    return (
        f"⚠️ У <b>{html.escape(label)}</b> уже есть ключ AmneziaWG {html.escape(country.short)}.\n"
        "Новый заменит старый — там, где стоит старый, связь пропадёт."
    )


def awg_replace_keyboard(key: str, node: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Выпустить новый",
                    callback_data=action_cb(vpn_protocol.ACTION_REISSUE, KIND_AWG, key, node),
                ),
                InlineKeyboardButton(text="Отмена", callback_data=card_cb(SCREEN_AWG, key)),
            ]
        ]
    )


def awg_file_caption(label: str, country: vd.Country) -> str:
    return f"⚡ {html.escape(label)} · AmneziaWG · {html.escape(country.short)}"


def awg_steps_text(label: str, country: vd.Country) -> str:
    rename = html.escape(f"{country.short} {label}".strip())
    return (
        "1. Установите AmneziaVPN — кнопки ниже.\n"
        "2. Нажмите на файл выше → «Открыть в AmneziaVPN».\n"
        f"3. ❗️ Обязательно переименуйте добавленное подключение — например, «{rename}»."
    )


def awg_steps_keyboard(cfg: VpnConfig, qr_callback: str | None) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="App Store", url=cfg.amneziavpn_ios_app_store_url),
            InlineKeyboardButton(text="Google Play", url=cfg.amneziavpn_google_play_url),
            InlineKeyboardButton(text="Сайт", url=cfg.official_download_url),
        ]
    ]
    if qr_callback:
        rows.append(
            [InlineKeyboardButton(text="📷 QR для другого устройства", callback_data=qr_callback)]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Магазин недоступен?",
                callback_data=commands.action_callback("apk", APK_NO_STORE, service=SERVICE),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def no_store_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Скачать AmneziaWG (.apk)",
                    callback_data=commands.action_callback("apk", "send", service=SERVICE),
                )
            ]
        ]
    )


# --- новое устройство ------------------------------------------------------


def new_device_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=text,
                callback_data=action_cb(vpn_protocol.ACTION_ISSUE, KIND_NEW, code),
            )
            for code, text in wizard.PICK_BUTTONS.items()
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=card_cb("m"))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def create_failed_keyboard(platform: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔁 Попробовать ещё раз",
                    callback_data=action_cb(vpn_protocol.ACTION_ISSUE, KIND_NEW, platform),
                )
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=card_cb("m"))],
        ]
    )


def back_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=card_cb(SCREEN_PICK, key))]
        ]
    )


# --- перевыпуск по выбранным подключениям (57.5) ----------------------------

MAX_REISSUE_BITS = 16
LIST_CHANGED_TEXT = "Список изменился, выберите заново."
NOTHING_PICKED_TEXT = "Ничего не выбрано."


def reissue_connections(device: vd.Device) -> list[vd.Connection]:
    """Выданные подключения в стабильном порядке (его задаёт ``build_devices``);
    бит маски = индекс в этом списке."""
    return [c for c in device.issued if c.transport in vd.TRANSPORT_NAME][:MAX_REISSUE_BITS]


def list_signature(conns: list[vd.Connection]) -> str:
    raw = "|".join(f"{c.node}/{c.transport}" for c in conns)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]


def all_mask(conns: list[vd.Connection]) -> int:
    return (1 << len(conns)) - 1


def selected(conns: list[vd.Connection], mask: int) -> list[vd.Connection]:
    return [c for i, c in enumerate(conns) if mask >> i & 1]


def parse_selection(rest: str) -> tuple[str, str | None, int]:
    """``<ключ>[-<подпись>-<маска>]`` → (ключ, подпись | None, маска); мусор — без выбора."""
    key, _, tail = rest.partition("-")
    sig, _, mask_hex = tail.partition("-")
    if not sig:
        return key, None, 0
    try:
        return key, sig, int(mask_hex, 16)
    except ValueError:
        return key, sig, 0


def reissue_select_cb(key: str, conns: list[vd.Connection], mask: int) -> str:
    return card_cb(SCREEN_REISSUE, f"{key}-{list_signature(conns)}-{mask:x}")


def reissue_run_cb(key: str, conns: list[vd.Connection], mask: int) -> str:
    return action_cb(
        vpn_protocol.ACTION_REISSUE, KIND_MULTI, f"{key}-{list_signature(conns)}-{mask:x}"
    )


def reissue_conn_text(conn: vd.Connection, countries: dict[str, vd.Country]) -> str:
    country = countries.get(conn.node)
    return f"{country.short if country else conn.node} {vd.TRANSPORT_NAME[conn.transport]}"


def reissue_select_text(label: str, note: str = "") -> str:
    head = (
        f"🔄 <b>{html.escape(label)}</b> — какие ключи перевыпустить?\n"
        "Старые перестанут работать сразу, новые настройки придут следом."
    )
    return f"{html.escape(note)}\n\n{head}" if note else head


def reissue_select_keyboard(
    device: vd.Device,
    conns: list[vd.Connection],
    countries: dict[str, vd.Country],
    mask: int,
    *,
    can_run: bool = True,
) -> InlineKeyboardMarkup:
    key = device.key
    rows: list[list[InlineKeyboardButton]] = []
    last_node: str | None = None
    for i, conn in enumerate(conns):
        mark = "☑️" if mask >> i & 1 else "☐"
        button = InlineKeyboardButton(
            text=f"{mark} {reissue_conn_text(conn, countries)}",
            callback_data=reissue_select_cb(key, conns, mask ^ (1 << i)),
        )
        if rows and conn.node == last_node:
            rows[-1].append(button)
        else:
            rows.append([button])
        last_node = conn.node
    everything = all_mask(conns)
    if everything and mask != everything:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Выбрать все", callback_data=reissue_select_cb(key, conns, everything)
                )
            ]
        )
    last: list[InlineKeyboardButton] = []
    count = len(selected(conns, mask))
    if count and can_run:
        last.append(
            InlineKeyboardButton(
                text=f"🔄 Перевыпустить ({count})", callback_data=reissue_run_cb(key, conns, mask)
            )
        )
    last.append(InlineKeyboardButton(text="Отмена", callback_data=_device_cb(key)))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def reissue_result_text(label: str, done: list[str], failed: list[tuple[str, str]]) -> str:
    lines = [f"🔄 <b>{html.escape(label)}</b>", ""]
    if done:
        lines.append("✅ Перевыпущено: " + ", ".join(html.escape(d) for d in done) + ".")
    if failed:
        lines.append("⚠️ Не удалось:")
        lines += [f"• {html.escape(name)} — {html.escape(why)}" for name, why in failed]
        lines.append("Повторите чуть позже.")
    if done:
        lines.append("Новые настройки — в сообщениях ниже.")
    return "\n".join(lines)


def reissue_result_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К устройству", callback_data=_device_cb(key))]
        ]
    )
