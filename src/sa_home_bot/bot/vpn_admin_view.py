from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import guest_rights, vpn_callbacks, vpn_format
from sa_home_bot.bot.pagination import nav_row
from sa_home_bot.bot.ui.keyboards import back_row, button, markup
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.vpn import protocol as vpn_protocol

SERVICE = vpn_callbacks.SERVICE
GUEST_PAGE_SIZE = 8

QUOTA_PRESETS_GB = (50, 100, 200, 500, 1000)
QUOTA_STEP_GB = 50

_MAX_NAME_LEN = 40

_SCREEN = vpn_protocol.ACTION_PEERS
_GUEST_PREFIX = "g"

_VPN_GROUP_KEY = "vpn"

ACTION_SET_RIGHTS = "set_rights"
_RIGHTS_PREFIX = "r"


@dataclass(frozen=True)
class RightToggle:
    key: str
    label: str
    rights: frozenset[str]
    note: str = ""


VPN_TOGGLES: tuple[RightToggle, ...] = (
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
    return vpn_callbacks.vpn_callback(_SCREEN, f"{_RIGHTS_PREFIX}{chat_id}")


def toggle_cb(chat_id: int, key: str) -> str:
    return vpn_callbacks.vpn_callback(ACTION_SET_RIGHTS, f"{chat_id}_{key}")


def set_access_cb(chat_id: int, node: str, arg: str) -> str:
    return vpn_callbacks.vpn_callback(vpn_protocol.ACTION_SET_ACCESS, f"{chat_id}_{arg}", node)


def parse_guest_value(value: str | None) -> int | None:
    if not value or not value.startswith(_GUEST_PREFIX):
        return None
    try:
        return int(value[len(_GUEST_PREFIX) :])
    except ValueError:
        return None


def parse_rights_value(value: str | None) -> int | None:
    if not value or not value.startswith(_RIGHTS_PREFIX):
        return None
    try:
        return int(value[len(_RIGHTS_PREFIX) :])
    except ValueError:
        return None


def parse_set_access_value(value: str | None) -> tuple[int, str] | None:
    if not value or "_" not in value:
        return None
    raw_chat, _, arg = value.partition("_")
    try:
        return int(raw_chat), arg
    except ValueError:
        return None


def server_name(server: dict) -> str:
    label = str(server.get("label") or "")
    return vpn_protocol.country_flag(label) or label or str(server.get("node") or "?")


def _access_mark(server: dict) -> str:
    return "✅" if server.get("allowed") else "⬜"


def has_any_vpn_right(sub: Subscription) -> bool:
    group = guest_rights.group(_VPN_GROUP_KEY)
    if group is None:
        return False
    return any(sub.allows_action(right.split("@", 1)[0], SERVICE) for right in group.rights)


def _sub_allows_right(sub: Subscription, right: str) -> bool:
    return sub.allows_action(right.split("@", 1)[0], SERVICE)


def toggle_state(sub: Subscription, toggle: RightToggle) -> bool:
    return all(_sub_allows_right(sub, right) for right in toggle.rights)


def toggle_mark(sub: Subscription, toggle: RightToggle) -> str:
    if toggle_state(sub, toggle):
        return "✅"
    return "🔸" if any(_sub_allows_right(sub, right) for right in toggle.rights) else "⬜"


def toggle_rights(sub: Subscription, toggle: RightToggle) -> frozenset[str]:
    if toggle_state(sub, toggle):
        return sub.allowed_commands - toggle.rights
    return sub.allowed_commands | toggle.rights


def toggle_by_key(key: str) -> RightToggle | None:
    return _TOGGLE_BY_KEY.get(key)


def sort_guests(guests: Sequence[Subscription]) -> list[Subscription]:
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
    buttons.append(back_row(vpn_callbacks.home_cb(), "⬅️ К карточке"))
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
    buttons.append(back_row(guest_cb(guest.chat_id)))
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
    buttons.append(back_row(guest_cb(chat_id)))
    return "\n".join(lines), markup(*buttons)
