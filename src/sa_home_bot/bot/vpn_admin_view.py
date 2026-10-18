"""Экраны «👥 Все гости» в /vpn: кому на какой локации открыт VPN и на
сколько гигабайт (решение пользователя 2026-09-18).

Здесь решается ГДЕ, СКОЛЬКО и ЧТО ИМЕННО можно гостю внутри VPN.

Разделение с /guests (решение пользователя 2026-09-24): там группа «📶 VPN»
выдаётся и снимается целиком — «пускаем ли вообще», — а тонкая настройка
живёт здесь. Тонкого две оси, и они разные по природе:

- ГДЕ и СКОЛЬКО — допуск к конкретной локации и её постоянная база в ГБ.
  Правом это не выражается и не может быть (§3.5 AUTHORIZATION.md): локации
  живут в рое, а не в списке строк конфига, и у каждой свой счёт за трафик.
  Хранится в самой службе — таблица `vpn_chat_access` каждой ноды.
- ЧТО ИМЕННО — тумблеры ``VPN_TOGGLES``: видеть карточку, управлять
  устройствами, докупать трафик, прокси Telegram. Это уже обычные права
  (`действие@vpn`) в гостевой подписке, правятся `Gatekeeper.set_guest_rights`.

Иерархия — «телефонное меню» в одном сообщении, как /guests
(bot/guests_view.py): список гостей → карточка гостя (его локации + кнопка
«🔐 Права VPN») → экран локации (тумблер доступа + гигабайты) либо экран прав
(тумблеры умений). Модуль только рендерит из готовых данных; сеть и запись —
в bot/handlers/vpn.py.

Права экранов — обычные `действие@vpn` (CallbackAuthorizationMiddleware):
смотреть — `peers@vpn`, менять допуск/квоту — `set_access@vpn`, менять
умения гостя — `set_rights@vpn`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import guest_rights, vpn_callbacks, vpn_format, vpn_keyboards
from sa_home_bot.bot.pagination import nav_row
from sa_home_bot.bot.ui.keyboards import button, markup
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_callbacks.SERVICE
GUEST_PAGE_SIZE = 8

# Ходовые размеры личной квоты. Свободный ввод числа потребовал бы FSM,
# которого в боте нет нигде, а эти пять значений закрывают живые случаи;
# точная цифра между ними набирается кнопками «±50».
QUOTA_PRESETS_GB = (50, 100, 200, 500, 1000)
QUOTA_STEP_GB = 50

_MAX_NAME_LEN = 40

# Экран (не операция) внутри админского раздела — отдельного действия службы
# под него не заводим, право то же, что у списка пиров: `peers@vpn`.
_SCREEN = vpn_protocol.ACTION_PEERS
_GUEST_PREFIX = "g"  # act:vpn:peers:g<chat_id>[_<нода>]

# Группа прав, по которой список сортируется (bot/guest_rights.py).
_VPN_GROUP_KEY = "vpn"

# Действие правки умений гостя. Службы у него нет — книга подписок живёт в
# боте, нода про неё не знает; имя нужно только как идентификатор права
# `set_rights@vpn` и как часть callback_data (тот же приём, что у `vpn_card`).
ACTION_SET_RIGHTS = "set_rights"
_RIGHTS_PREFIX = "r"  # act:vpn:peers:r<chat_id>


@dataclass(frozen=True)
class RightToggle:
    """Умение внутри VPN — одной кнопкой, хотя прав за ним несколько.

    Право поштучно здесь не тумблер: «выпустить устройство» без «перевыпустить»
    и «отозвать» — не половина умения, а сломанное умение (гость выпустит ключ
    и не сможет его сменить). Поэтому тумблер = рабочий комплект, а поштучная
    правка остаётся доступной руками в config.toml для владельческих подписок.
    """

    key: str  # короткий id, едет в callback_data
    label: str
    rights: frozenset[str]
    note: str = ""


VPN_TOGGLES: tuple[RightToggle, ...] = (
    # Входной билет: без usage@vpn команда /vpn гостю недоступна вовсе
    # (bot/commands.py::VPN), поэтому снятие этого тумблера гасит и все
    # остальные экраны — про это прямо сказано на странице.
    RightToggle(
        key="see",
        label="👁 Видеть карточку /vpn",
        rights=frozenset({"usage@vpn", "vpn_card@vpn"}),
        note="Без этого /vpn недоступна, и остальные умения ему не открыть.",
    ),
    RightToggle(
        key="dev",
        label="📱 Свои устройства",
        rights=frozenset({"issue@vpn", "reissue@vpn", "revoke@vpn", "apk@vpn"}),
        note="Выпустить, перевыпустить, отозвать, получить приложение.",
    ),
    RightToggle(
        key="gb",
        label="➕ Трафик",
        rights=frozenset({"grant_extra@vpn", "request_extra@vpn"}),
        note="Докупить +100 ГБ самому и попросить сверх потолка.",
    ),
    RightToggle(
        key="proxy",
        label="✈️ Прокси Telegram",
        rights=frozenset({"proxy_link@vpn"}),
        note="Только Telegram, не VPN. Допуска к локации и квоты не требует.",
    ),
)

_TOGGLE_BY_KEY = {toggle.key: toggle for toggle in VPN_TOGGLES}


def guests_cb(offset: int = 0) -> str:
    return vpn_callbacks.vpn_callback(_SCREEN, str(offset))


def guest_cb(chat_id: int) -> str:
    return vpn_callbacks.vpn_callback(_SCREEN, f"{_GUEST_PREFIX}{chat_id}")


def location_cb(chat_id: int, node: str) -> str:
    return vpn_callbacks.vpn_callback(_SCREEN, f"{_GUEST_PREFIX}{chat_id}", node)


def rights_cb(chat_id: int) -> str:
    """Экран умений гостя — читается под тем же `peers@vpn`, что и остальной
    админский раздел: это тот же просмотр, только другой срез."""
    return vpn_callbacks.vpn_callback(_SCREEN, f"{_RIGHTS_PREFIX}{chat_id}")


def toggle_cb(chat_id: int, key: str) -> str:
    """``act:vpn:set_rights:<chat_id>_<ключ тумблера>`` — право `set_rights@vpn`.

    Что включить, а что выключить, кнопка не несёт: состояние читается из
    подписки в момент нажатия (``toggle_rights``). Иначе две вкладки одного
    экрана у владельца разъезжались бы — нажатая на устаревшей кнопке
    «выключить» сняла бы право, которое он только что выдал с другой.
    """
    return vpn_callbacks.vpn_callback(ACTION_SET_RIGHTS, f"{chat_id}_{key}")


def set_access_cb(chat_id: int, node: str, arg: str) -> str:
    """``act:vpn:set_access:<chat_id>_<arg>:<нода>`` — arg это ``on``/``off``
    либо число гигабайт. Составное значение — принятая в vpn.py конвенция
    (см. resolve_request)."""
    return vpn_callbacks.vpn_callback(vpn_protocol.ACTION_SET_ACCESS, f"{chat_id}_{arg}", node)


def parse_guest_value(value: str | None) -> int | None:
    """``g<chat_id>`` → chat_id (None — это не экран гостя, а номер страницы)."""
    if not value or not value.startswith(_GUEST_PREFIX):
        return None
    try:
        return int(value[len(_GUEST_PREFIX) :])
    except ValueError:
        return None


def parse_rights_value(value: str | None) -> int | None:
    """``r<chat_id>`` → chat_id (None — это не экран умений)."""
    if not value or not value.startswith(_RIGHTS_PREFIX):
        return None
    try:
        return int(value[len(_RIGHTS_PREFIX) :])
    except ValueError:
        return None


def parse_set_access_value(value: str | None) -> tuple[int, str] | None:
    """``<chat_id>_<arg>`` → (chat_id, arg)."""
    if not value or "_" not in value:
        return None
    raw_chat, _, arg = value.partition("_")
    try:
        return int(raw_chat), arg
    except ValueError:
        return None


def server_name(server: dict) -> str:
    """Как назвать локацию на этих экранах — флагом, если он в метке есть.

    `[vpn].location` — это «🇳🇱 Нидерланды», и слово рядом с флагом ничего не
    добавляет: флаг уже называет страну (решение владельца 2026-09-19).
    Метка без флага (или её отсутствие) показывается как есть — там сокращать
    нечего.
    """
    label = str(server.get("label") or "")
    return vpn_protocol.country_flag(label) or label or str(server.get("node") or "?")


def _access_mark(server: dict) -> str:
    return "✅" if server.get("allowed") else "⬜"


def has_any_vpn_right(sub: Subscription) -> bool:
    """Выдана ли чату группа «📶 VPN» (хоть одно право из неё).

    Хоть одно, а не весь набор: группу могли доправить руками в конфиге или
    снять одно право — человек всё равно «по VPN». Проверка идёт через
    ``allows_action``, поэтому `*` и `*@vpn` тоже считаются.
    """
    group = guest_rights.group(_VPN_GROUP_KEY)
    if group is None:
        return False
    return any(sub.allows_action(right.split("@", 1)[0], SERVICE) for right in group.rights)


def _sub_allows_right(sub: Subscription, right: str) -> bool:
    """Считаем по ``allows_action``, а не вхождением строки в
    ``allowed_commands``: владельцу с `*` или `*@vpn` (config.toml) экран
    должен показывать включённое, а не пустые квадратики."""
    return sub.allows_action(right.split("@", 1)[0], SERVICE)


def toggle_state(sub: Subscription, toggle: RightToggle) -> bool:
    """Выдан ли комплект ЦЕЛИКОМ."""
    return all(_sub_allows_right(sub, right) for right in toggle.rights)


def toggle_mark(sub: Subscription, toggle: RightToggle) -> str:
    """✅ весь комплект · 🔸 неполный · ⬜ ничего.

    Неполный — не теоретический случай: гости, впущенные до появления
    `vpn_card@vpn` в группе, живут с `usage@vpn` без него, и показывать им
    пустой квадратик было бы неправдой — карточка-то открывается.
    """
    if toggle_state(sub, toggle):
        return "✅"
    return "🔸" if any(_sub_allows_right(sub, right) for right in toggle.rights) else "⬜"


def toggle_rights(sub: Subscription, toggle: RightToggle) -> frozenset[str]:
    """Новый набор прав гостя после нажатия: снять, если комплект выдан
    целиком, иначе дожать его до полного.

    Неполный набор дожимается, а не снимается, — тот же приём, что на странице
    «Добавить право» в /guests: частичный комплект (выдали issue, не выдали
    reissue) — это сломанное умение, и вероятнее, что его хотят починить, а не
    добить. Чтобы снять — нажать второй раз, уже на полном.
    """
    if toggle_state(sub, toggle):
        return sub.allowed_commands - toggle.rights
    return sub.allowed_commands | toggle.rights


def toggle_by_key(key: str) -> RightToggle | None:
    return _TOGGLE_BY_KEY.get(key)


def sort_guests(guests: Sequence[Subscription]) -> list[Subscription]:
    """Сначала те, кому группа VPN выдана — с ними тут и работают; остальные
    ниже, но видны (кому-то из них доступ как раз и собираются открыть).
    Сортировка устойчивая, внутри половин порядок книги подписок не меняется.
    """
    return sorted(guests, key=lambda sub: not has_any_vpn_right(sub))


def build_guests_view(
    guests: Sequence[Subscription], access: dict[int, list[dict]], offset: int
) -> tuple[str, InlineKeyboardMarkup]:
    guests = sort_guests(guests)
    lines = [f"👥 <b>Гости VPN</b> ({len(guests)})", ""]
    if not guests:
        lines.append("Гостей нет.")
    page = guests[offset : offset + GUEST_PAGE_SIZE]
    for guest in page:
        servers = access.get(guest.chat_id) or []
        opened = [s for s in servers if s.get("allowed")]
        if opened:
            where = ", ".join(
                f"{server_name(s)} {vpn_format.bytes_to_gb(s.get('used_bytes', 0)):.0f}/"
                f"{vpn_format.bytes_to_gb(s.get('limit_bytes', 0)):.0f} ГБ"
                for s in opened
            )
        else:
            where = "доступа нет"
        lines.append(f"• {escape(guest.name)} — {escape(where)}")
    buttons = [
        [button(f"👤 {guest.name}"[:_MAX_NAME_LEN], guest_cb(guest.chat_id))] for guest in page
    ]
    nav = nav_row(offset, GUEST_PAGE_SIZE, len(guests), guests_cb)
    if nav:
        buttons.append(nav)
    buttons.append(vpn_keyboards.back_row(vpn_callbacks.home_cb(), "⬅️ К карточке"))
    return "\n".join(lines), markup(*buttons)


def build_guest_view(
    guest: Subscription, servers: Sequence[dict]
) -> tuple[str, InlineKeyboardMarkup]:
    enabled = [
        f"{toggle.label}{'' if toggle_state(guest, toggle) else ' (неполно)'}"
        for toggle in VPN_TOGGLES
        if toggle_mark(guest, toggle) != "⬜"
    ]
    lines = [
        f"👤 <b>{escape(guest.name)}</b>",
        "",
        f"chat_id: <code>{guest.chat_id}</code>",
        "Умения: " + (escape(", ".join(enabled)) if enabled else "нет"),
        "",
    ]
    if not servers:
        lines.append("Ни одна нода с VPN сейчас не на связи.")
    for server in servers:
        used = vpn_format.bytes_to_gb(server.get("used_bytes", 0))
        limit = vpn_format.bytes_to_gb(server.get("limit_bytes", 0))
        if server.get("allowed"):
            base = vpn_format.bytes_to_gb(server.get("base_limit_bytes", 0))
            personal = " (личная)" if server.get("personal_base") else " (общая)"
            lines.append(
                f"{_access_mark(server)} <b>{escape(server_name(server))}</b>: "
                f"{used:.1f} / {limit:.0f} ГБ, база {base:.0f} ГБ{personal}"
            )
        else:
            lines.append(f"{_access_mark(server)} <b>{escape(server_name(server))}</b>: закрыт")
    buttons = [
        [
            button(
                f"{_access_mark(server)} {server_name(server)}"[:_MAX_NAME_LEN],
                location_cb(guest.chat_id, str(server.get("node"))),
            )
        ]
        for server in servers
        if server.get("node")
    ]
    buttons.append([button("🔐 Права VPN", rights_cb(guest.chat_id))])
    buttons.append([button("⬅️ К списку", guests_cb(0))])
    return "\n".join(lines), markup(*buttons)


def build_rights_view(guest: Subscription) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"🔐 <b>Права VPN — {escape(guest.name)}</b>",
        "",
    ]
    for toggle in VPN_TOGGLES:
        lines.append(f"{toggle_mark(guest, toggle)} <b>{escape(toggle.label)}</b>")
        if toggle.note:
            lines.append(f"   <i>{escape(toggle.note)}</i>")
    lines.append("")
    lines.append("Правится сразу, без перезапуска.")
    buttons = [
        [
            button(
                f"{toggle_mark(guest, toggle)} {toggle.label}"[:_MAX_NAME_LEN],
                toggle_cb(guest.chat_id, toggle.key),
            )
        ]
        for toggle in VPN_TOGGLES
    ]
    buttons.append(vpn_keyboards.back_row(guest_cb(guest.chat_id)))
    return "\n".join(lines), markup(*buttons)


def _preset_rows(chat_id: int, node: str, base_gb: float) -> list[list[InlineKeyboardButton]]:
    buttons = [
        button(
            f"{'•' if abs(preset - base_gb) < 0.5 else ''}{preset} ГБ",
            set_access_cb(chat_id, node, str(preset)),
        )
        for preset in QUOTA_PRESETS_GB
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append(
        [
            button(
                f"➖{QUOTA_STEP_GB}",
                set_access_cb(chat_id, node, str(max(QUOTA_STEP_GB, int(base_gb) - QUOTA_STEP_GB))),
            ),
            button(
                f"➕{QUOTA_STEP_GB}",
                set_access_cb(chat_id, node, str(int(base_gb) + QUOTA_STEP_GB)),
            ),
        ]
    )
    return rows


def build_location_view(guest: Subscription, server: dict) -> tuple[str, InlineKeyboardMarkup]:
    chat_id = guest.chat_id
    node = str(server.get("node"))
    allowed = bool(server.get("allowed"))
    base_gb = vpn_format.bytes_to_gb(server.get("base_limit_bytes", 0))
    lines = [
        f"📶 <b>{escape(server_name(server))}</b> — {escape(guest.name)}",
        "",
        "Доступ: " + ("✅ открыт" if allowed else "⬜ закрыт"),
        f"База: {base_gb:.0f} ГБ в месяц"
        + (" (личная)" if server.get("personal_base") else " (общая)"),
        f"Израсходовано: {vpn_format.bytes_to_gb(server.get('used_bytes', 0)):.1f} / "
        f"{vpn_format.bytes_to_gb(server.get('limit_bytes', 0)):.0f} ГБ",
    ]
    if allowed:
        lines.append("")
        lines.append("Гигабайты — это постоянная база: каждый месяц столько же.")
    buttons: list[list[InlineKeyboardButton]] = [
        [
            button(
                "⬜ Закрыть доступ" if allowed else "✅ Открыть доступ",
                set_access_cb(chat_id, node, "off" if allowed else "on"),
            )
        ]
    ]
    if allowed:
        buttons.extend(_preset_rows(chat_id, node, base_gb))
    buttons.append(vpn_keyboards.back_row(guest_cb(chat_id)))
    return "\n".join(lines), markup(*buttons)
