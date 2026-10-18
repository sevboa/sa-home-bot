from __future__ import annotations

import hashlib
import html

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import vpn_callbacks, vpn_keyboards
from sa_home_bot.bot import vpn_devices as vd
from sa_home_bot.bot import vpn_wizard as wizard
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.config import VpnConfig
from sa_home_bot.domain import vpn_check
from sa_home_bot.vpn import protocol as vpn_protocol

_TRANSPORT_CODE = {vd.REALITY: "v", vd.AWG: "a"}

APK_NO_STORE = "nostore"
APK_SEND = "send"
MAY_NOT_WORK = "сейчас может не работать"

NO_STORE_TEXT = (
    "Можно временно поставить приложение AmneziaWG файлом — лучше, чем ничего. "
    "Подключитесь через него, затем скачайте настоящий AmneziaVPN и перенесите туда настройки."
)
UNAVAILABLE_TEXT = "Не получилось подготовить настройки — сервер не ответил.\nПовторите чуть позже."
NO_AWG_TEXT = "AmneziaVPN сейчас не выдаётся ни в одной стране."
VLESS_FILE_CAPTION = "Настройки Hiddify. Нажмите на файл и выберите «Hiddify»."


PAGE_BUTTON = "📶 Открыть настройки"
PAGE_CARD_BUTTON = "📶 Настройки на странице"
OTHER_WAYS_BUTTON = "Другие способы"
CANCEL_TEXT = "Отмена"
TO_DEVICE_TEXT = "⬅️ К устройству"

MAX_REISSUE_BITS = 16
LIST_CHANGED_TEXT = "Список изменился, выберите заново."
NOTHING_PICKED_TEXT = "Ничего не выбрано."

page_link_text = wizard.page_link_text


def _device_cb(key: str) -> str:
    return vpn_callbacks.card_cb(vpn_callbacks.SCREEN_DEVICE, key)


def _issue_cb(kind: str, key: str, node: str | None = None) -> str:
    return vpn_callbacks.expert_cb(vpn_protocol.ACTION_ISSUE, kind, key, node)


def page_link_keyboard(page_url: str) -> InlineKeyboardMarkup:
    return markup([InlineKeyboardButton(text=PAGE_BUTTON, url=page_url)])


def new_device_link_keyboard(page_url: str, key: str) -> InlineKeyboardMarkup:
    return markup(
        [InlineKeyboardButton(text=PAGE_BUTTON, url=page_url)],
        [button(TO_DEVICE_TEXT, _device_cb(key))],
    )


def pick_text(label: str) -> str:
    return f"📥 <b>{html.escape(label)}</b> — чем подключаться?"


def pick_keyboard(key: str) -> InlineKeyboardMarkup:
    return markup(
        [button("⚡ AmneziaVPN", vpn_callbacks.card_cb(vpn_callbacks.SCREEN_AWG, key))],
        [button(f"🛡 {vd.TRANSPORT_NAME[vd.REALITY]}", _issue_cb(vpn_callbacks.KIND_VLESS, key))],
        vpn_keyboards.back_row(_device_cb(key)),
    )


def had_vless_handshake(device: vd.Device) -> bool:
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
    rows: list[list[InlineKeyboardButton]] = []
    if page_url:
        rows.append([InlineKeyboardButton(text="🔌 Подключить", url=page_url)])
    if file_again_key:
        rows.append(
            [button("⚙️ Файл настроек ещё раз", _issue_cb(vpn_callbacks.KIND_FILE, file_again_key))]
        )
    if qr_buttons:
        rows.append([button(text, data) for text, data in qr_buttons])
    return markup(*rows)


def qr_button_text(country: vd.Country, multi: bool) -> str:
    return f"📷 QR {country.short}" if multi else "📷 QR"


def awg_pick_text(label: str) -> str:
    return f"⚡ <b>{html.escape(label)}</b> · AmneziaVPN — в какой стране?"


def awg_country_button_text(server: dict) -> str:
    name = server.get("label") or server.get("node") or "?"
    health = vd.transport_health(server, vd.AWG)
    if health is not None and health != vpn_check.OK:
        return f"{name} — {MAY_NOT_WORK}"
    return name


def awg_pick_keyboard(key: str, servers: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                awg_country_button_text(server),
                _issue_cb(vpn_callbacks.KIND_AWG_COUNTRY, key, server["node"]),
            )
        ]
        for server in servers
    ]
    rows.append(vpn_keyboards.back_row(vpn_callbacks.card_cb(vpn_callbacks.SCREEN_PICK, key)))
    return markup(*rows)


def awg_replace_text(label: str, country: vd.Country) -> str:
    return (
        f"⚠️ У <b>{html.escape(label)}</b> уже есть ключ AmneziaVPN {html.escape(country.short)}.\n"
        "Новый заменит старый — там, где стоит старый, связь пропадёт."
    )


def awg_replace_keyboard(key: str, node: str) -> InlineKeyboardMarkup:
    return markup(
        [
            button(
                "Выпустить новый",
                vpn_callbacks.expert_cb(
                    vpn_protocol.ACTION_REISSUE, vpn_callbacks.KIND_AWG_COUNTRY, key, node
                ),
            ),
            button(CANCEL_TEXT, vpn_callbacks.card_cb(vpn_callbacks.SCREEN_AWG, key)),
        ]
    )


def awg_file_caption(label: str, country: vd.Country) -> str:
    return f"⚡ {html.escape(label)} · AmneziaVPN · {html.escape(country.short)}"


def awg_steps_text(label: str, country: vd.Country) -> str:
    rename = html.escape(f"{country.short} {label}".strip())
    return (
        "1. Установите именно AmneziaVPN — кнопки ниже (не AmneziaWG).\n"
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
        rows.append([button("📷 QR для другого устройства", qr_callback)])
    rows.append(
        [
            button(
                "Магазин недоступен?",
                vpn_callbacks.vpn_callback(vpn_protocol.ACTION_VPN_APK, APK_NO_STORE),
            )
        ]
    )
    return markup(*rows)


def no_store_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [
            button(
                "📦 Скачать AmneziaWG (.apk)",
                vpn_callbacks.vpn_callback(vpn_protocol.ACTION_VPN_APK, APK_SEND),
            )
        ]
    )


def new_device_keyboard() -> InlineKeyboardMarkup:
    return markup(
        [
            button(text, _issue_cb(vpn_callbacks.KIND_NEW, code))
            for code, text in wizard.PICK_BUTTONS.items()
        ],
        vpn_keyboards.back_row(vpn_callbacks.card_cb(vpn_callbacks.SCREEN_LIST)),
    )


def create_failed_keyboard(platform: str) -> InlineKeyboardMarkup:
    return markup(
        [button(vpn_keyboards.RETRY_TEXT, _issue_cb(vpn_callbacks.KIND_NEW, platform))],
        vpn_keyboards.back_row(vpn_callbacks.card_cb(vpn_callbacks.SCREEN_LIST)),
    )


def back_keyboard(key: str) -> InlineKeyboardMarkup:
    return markup(vpn_keyboards.back_row(vpn_callbacks.card_cb(vpn_callbacks.SCREEN_PICK, key)))


def reissue_connections(device: vd.Device) -> list[vd.Connection]:
    return [c for c in device.issued if c.transport in vd.TRANSPORT_NAME][:MAX_REISSUE_BITS]


def list_signature(conns: list[vd.Connection]) -> str:
    raw = "|".join(f"{c.node}/{c.transport}" for c in conns)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:4]


def all_mask(conns: list[vd.Connection]) -> int:
    return (1 << len(conns)) - 1


def selected(conns: list[vd.Connection], mask: int) -> list[vd.Connection]:
    return [c for i, c in enumerate(conns) if mask >> i & 1]


def parse_selection(rest: str) -> tuple[str, str | None, int]:
    key, _, tail = rest.partition("-")
    sig, _, mask_hex = tail.partition("-")
    if not sig:
        return key, None, 0
    try:
        return key, sig, int(mask_hex, 16)
    except ValueError:
        return key, sig, 0


def _selection_value(key: str, conns: list[vd.Connection], mask: int) -> str:
    return f"{key}-{list_signature(conns)}-{mask:x}"


def reissue_select_cb(key: str, conns: list[vd.Connection], mask: int) -> str:
    return vpn_callbacks.card_cb(vpn_callbacks.SCREEN_REISSUE, _selection_value(key, conns, mask))


def reissue_run_cb(key: str, conns: list[vd.Connection], mask: int) -> str:
    return vpn_callbacks.expert_cb(
        vpn_protocol.ACTION_REISSUE, vpn_callbacks.KIND_MULTI, _selection_value(key, conns, mask)
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
        toggle = button(
            f"{mark} {reissue_conn_text(conn, countries)}",
            reissue_select_cb(key, conns, mask ^ (1 << i)),
        )
        if rows and conn.node == last_node:
            rows[-1].append(toggle)
        else:
            rows.append([toggle])
        last_node = conn.node
    everything = all_mask(conns)
    if everything and mask != everything:
        rows.append([button("Выбрать все", reissue_select_cb(key, conns, everything))])
    last: list[InlineKeyboardButton] = []
    count = len(selected(conns, mask))
    if count and can_run:
        last.append(button(f"🔄 Перевыпустить ({count})", reissue_run_cb(key, conns, mask)))
    last.append(button(CANCEL_TEXT, _device_cb(key)))
    rows.append(last)
    return markup(*rows)


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
    return markup([button(TO_DEVICE_TEXT, _device_cb(key))])


def node_hash(node: str) -> str:
    return hashlib.sha1(node.encode("utf-8")).hexdigest()[:4]


def restore_cb(key: str, node: str, transports: list[str]) -> str:
    codes = "".join(_TRANSPORT_CODE[t] for t in vd.TRANSPORT_ORDER if t in transports)
    return vpn_callbacks.card_cb(vpn_callbacks.SCREEN_RESTORE, f"{key}-{node_hash(node)}-{codes}")


def parse_restore(rest: str) -> tuple[str, str, str]:
    key, _, tail = rest.partition("-")
    nh, _, codes = tail.partition("-")
    return key, nh, codes


def restore_mask(conns: list[vd.Connection], nh: str, codes: str) -> int:
    mask = 0
    for i, conn in enumerate(conns):
        if node_hash(conn.node) == nh and _TRANSPORT_CODE.get(conn.transport) in codes:
            mask |= 1 << i
    return mask
