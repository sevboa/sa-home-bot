from __future__ import annotations

from collections.abc import Sequence
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, guest_rights
from sa_home_bot.bot.invites import format_code
from sa_home_bot.bot.pagination import clamp_offset, nav_row  # noqa: F401
from sa_home_bot.subscriptions.models import Subscription
from sa_home_bot.utils.timefmt import format_local

GUEST_PAGE_SIZE = 10
CODE_PAGE_SIZE = 5
PERM_PAGE_SIZE = 6

_MAX_NAME_LEN = 40


def format_local_time(iso: str) -> str:
    return format_local(iso, "%Y-%m-%d %H:%M", tolerant=True)


def build_list_view(
    guests: Sequence[Subscription], offset: int
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"<b>Приглашённые</b> ({len(guests)})", ""]
    if not guests:
        lines.append("Пока никого.")
    page = guests[offset : offset + GUEST_PAGE_SIZE]
    for guest in page:
        lines.append(
            f"• {escape(guest.name)} — <code>{guest.chat_id}</code>"
            + (f", вход {format_local_time(guest.invited_at)}" if guest.invited_at else "")
        )
    buttons = [
        [
            InlineKeyboardButton(
                text=f"👤 {guest.name}"[:_MAX_NAME_LEN],
                callback_data=commands.panel_callback(commands.GUEST_CARD_CODE, guest.chat_id),
            )
        ]
        for guest in page
    ]
    nav = nav_row(
        offset,
        GUEST_PAGE_SIZE,
        len(guests),
        lambda o: commands.panel_callback(commands.GUESTS_LIST_CODE, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔑 Открытые коды",
                callback_data=commands.panel_callback(commands.OPEN_CODES_LIST_CODE, 0),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_codes_view(open_codes: Sequence[dict], offset: int) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"<b>Открытые коды</b> ({len(open_codes)})", ""]
    if not open_codes:
        lines.append("Открытых кодов нет.")
    page = open_codes[offset : offset + CODE_PAGE_SIZE]
    for row in page:
        lines.append(
            f"• <code>{format_code(row['code'])}</code> — до {format_local_time(row['expires_at'])}"
        )
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔒 Отозвать {format_code(row['code'])}",
                callback_data=commands.panel_callback(commands.CODE_REVOKE_CODE, row["code"]),
            )
        ]
        for row in page
    ]
    nav = nav_row(
        offset,
        CODE_PAGE_SIZE,
        len(open_codes),
        lambda o: commands.panel_callback(commands.OPEN_CODES_LIST_CODE, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ К списку гостей",
                callback_data=commands.panel_callback(commands.GUESTS_LIST_CODE, 0),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_card_view(sub: Subscription) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"👤 <b>{escape(sub.name)}</b>", "", f"chat_id: <code>{sub.chat_id}</code>"]
    if sub.invited_at:
        lines.append(f"Вход: {format_local_time(sub.invited_at)}")
    if sub.invited_user:
        lines.append(f"Представился: {escape(sub.invited_user)}")
    lines.append(f"Прав выдано: {len(sub.allowed_commands)}")
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔐 Права ({len(sub.allowed_commands)})",
                callback_data=commands.panel_callback(commands.GUEST_PERMS_CODE, sub.chat_id, 0),
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Статистика VPN",
                callback_data=commands.panel_callback(commands.GUEST_STATS_CODE, sub.chat_id),
            )
        ],
        [
            InlineKeyboardButton(
                text="🚪 Выставить",
                callback_data=commands.panel_callback(
                    commands.GUEST_KICK_CONFIRM_CODE, sub.chat_id
                ),
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=commands.panel_callback(commands.GUESTS_LIST_CODE, 0)
            )
        ],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def build_kick_confirm_view(sub: Subscription) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"⚠️ Точно выставить <b>{escape(sub.name)}</b> (<code>{sub.chat_id}</code>)?\n"
        "Доступ пропадёт немедленно."
    )
    buttons = [
        [
            InlineKeyboardButton(
                text="✅ Да, выставить",
                callback_data=commands.panel_callback(commands.GUEST_REVOKE_CODE, sub.chat_id),
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Отмена",
                callback_data=commands.panel_callback(commands.GUEST_CARD_CODE, sub.chat_id),
            )
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def build_back_to_card_view(
    sub_name: str, chat_id: int, body: str
) -> tuple[str, InlineKeyboardMarkup]:
    text = f"📊 <b>{escape(sub_name)}</b>\n\n{body}"
    buttons = [
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.panel_callback(commands.GUEST_CARD_CODE, chat_id),
            )
        ]
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def _granted_entries(sub: Subscription) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    grouped: set[str] = set()
    for group in guest_rights.GUEST_GROUPS:
        granted = group.rights & sub.allowed_commands
        if not granted:
            continue
        grouped |= granted
        total = len(group.rights)
        suffix = "" if len(granted) == total else f" ({len(granted)}/{total})"
        entries.append((f"{group.label}{suffix}", commands.GUEST_GROUP_OFF_CODE, group.key))
    for right in sorted(sub.allowed_commands - grouped):
        entries.append((guest_rights.label(right), commands.GUEST_PERM_OFF_CODE, right))
    return entries


def granted_rows(sub: Subscription) -> int:
    return len(_granted_entries(sub))


def build_perms_view(sub: Subscription, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    entries = _granted_entries(sub)
    lines = [f"🔐 <b>Права гостя {escape(sub.name)}</b> ({len(sub.allowed_commands)})", ""]
    if not entries:
        lines.append("Прав нет.")
    page = entries[offset : offset + PERM_PAGE_SIZE]
    for text, _code, _arg in page:
        lines.append(f"• {escape(text)}")
    buttons = [
        [
            InlineKeyboardButton(
                text=f"➖ {text}"[:_MAX_NAME_LEN],
                callback_data=commands.panel_callback(code, sub.chat_id, offset, arg),
            )
        ]
        for text, code, arg in page
    ]
    nav = nav_row(
        offset,
        PERM_PAGE_SIZE,
        len(entries),
        lambda o: commands.panel_callback(commands.GUEST_PERMS_CODE, sub.chat_id, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить право",
                callback_data=commands.panel_callback(
                    commands.GUEST_PERM_ADD_LIST_CODE, sub.chat_id, 0
                ),
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.panel_callback(commands.GUEST_CARD_CODE, sub.chat_id),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _addable_entries(sub: Subscription) -> list[tuple[str, str, str, str]]:
    entries: list[tuple[str, str, str, str]] = [
        (group.label, commands.GUEST_GROUP_ADD_CODE, group.key, group.note)
        for group in guest_rights.GUEST_GROUPS
        if not group.rights <= sub.allowed_commands
    ]
    entries += [
        (item.label, commands.GUEST_PERM_ADD_CODE, item.right, "")
        for item in guest_rights.GUEST_RIGHTS
        if item.right not in sub.allowed_commands
    ]
    return entries


def build_perm_add_view(sub: Subscription, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    addable = _addable_entries(sub)
    lines = [f"➕ <b>Добавить право — {escape(sub.name)}</b>", ""]
    if not addable:
        lines.append("Все известные права уже выданы.")
    else:
        lines.append("Право выдаётся сразу, без перезапуска.")
    page = addable[offset : offset + PERM_PAGE_SIZE]
    for text, _code, _arg, note in page:
        if note:
            lines.append("")
            lines.append(f"{escape(text)} — {escape(note)}")
    buttons = [
        [
            InlineKeyboardButton(
                text=f"➕ {text}"[:_MAX_NAME_LEN],
                callback_data=commands.panel_callback(code, sub.chat_id, offset, arg),
            )
        ]
        for text, code, arg, _note in page
    ]
    nav = nav_row(
        offset,
        PERM_PAGE_SIZE,
        len(addable),
        lambda o: commands.panel_callback(commands.GUEST_PERM_ADD_LIST_CODE, sub.chat_id, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=commands.panel_callback(commands.GUEST_PERMS_CODE, sub.chat_id, 0),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
