"""Иерархия /guests (AUTHORIZATION.md §10) — «телефонное меню» в одном
сообщении: список гостей → карточка гостя → права → добавление права.

Решение пользователя 2026-08-04: вместо плоских карточек с новыми
сообщениями на каждый шаг — список с пагинацией, кнопки открывают вложенные
экраны РЕДАКТИРОВАНИЕМ того же сообщения (``edit_text``), «Назад» ведёт на
уровень выше. Права гостя (§3.2/§3.3) теперь можно листать, отключать
по одному и добавлять из каталога (bot/guest_rights.py) без перезапуска —
это сняло инвариант АВТОРИЗАЦИИ §8.11 в его прежней редакции («права
командой не выдаются»): гостевые подписки и так уже мутабельны в рантайме
(revoke_guest), точечная правка — тот же приём.

Модуль только рендерит (текст + клавиатура) из уже готовых данных; сетевые
вызовы (VPN usage) и запись состояния — в bot/handlers/invites.py. Права
проверяет CallbackAuthorizationMiddleware по общей записи в
commands._ALL_CALLBACK_ACTIONS (право GUESTS = `invite`, как и раньше).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from sa_home_bot.bot import commands, guest_rights
from sa_home_bot.bot.invites import format_code
from sa_home_bot.bot.pagination import clamp_offset, nav_row  # noqa: F401 — реэкспорт
from sa_home_bot.subscriptions.models import Subscription

# Гостей и прав на странице немного (домашний контур, не десятки людей) —
# 5/6 в ряд достаточно, чтобы страница не растягивалась на экран без надобности.
# Гостей — 10: список уже перевалил за десяток (живой рост 2026-08-06),
# при 5 на страницу пришлось бы листать втрое.
GUEST_PAGE_SIZE = 10
CODE_PAGE_SIZE = 5
PERM_PAGE_SIZE = 6

_MAX_NAME_LEN = 40  # с запасом под эмодзи-префикс кнопки, лимит Telegram куда шире


def format_local_time(iso: str) -> str:
    """UTC-метка из базы → местное время (то же соглашение, что везде в боте).

    Мусор (не ISO-строка) не роняет экран — показывается как есть.
    """
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _cb(code: str, *parts: object) -> str:
    return ":".join([commands.CALLBACK_PREFIX, code, *(str(p) for p in parts)])


# --- список гостей ------------------------------------------------------


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
                callback_data=_cb(commands.GUEST_CARD_CODE, guest.chat_id),
            )
        ]
        for guest in page
    ]
    nav = nav_row(
        offset, GUEST_PAGE_SIZE, len(guests), lambda o: _cb(commands.GUESTS_LIST_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="🔑 Открытые коды", callback_data=_cb(commands.OPEN_CODES_LIST_CODE, 0)
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# --- открытые коды --------------------------------------------------------


def build_codes_view(open_codes: Sequence[dict], offset: int) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"<b>Открытые коды</b> ({len(open_codes)})", ""]
    if not open_codes:
        lines.append("Открытых кодов нет.")
    page = open_codes[offset : offset + CODE_PAGE_SIZE]
    for row in page:
        lines.append(
            f"• <code>{format_code(row['code'])}</code> — до "
            f"{format_local_time(row['expires_at'])}"
        )
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔒 Отозвать {format_code(row['code'])}",
                callback_data=_cb(commands.CODE_REVOKE_CODE, row["code"]),
            )
        ]
        for row in page
    ]
    nav = nav_row(
        offset, CODE_PAGE_SIZE, len(open_codes), lambda o: _cb(commands.OPEN_CODES_LIST_CODE, o)
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ К списку гостей", callback_data=_cb(commands.GUESTS_LIST_CODE, 0)
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


# --- карточка гостя --------------------------------------------------------


def build_card_view(sub: Subscription) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"👤 <b>{escape(sub.name)}</b>", "", f"chat_id: <code>{sub.chat_id}</code>"]
    if sub.invited_at:
        lines.append(f"Вход: {format_local_time(sub.invited_at)}")
    if sub.invited_user:
        lines.append(f"Представился: {escape(sub.invited_user)}")
    lines.append(f"Прав выдано: {len(sub.allowed_commands)}")
    lines.append("🏠 Семья: да" if sub.family else "🏠 Семья: нет")
    buttons = [
        [
            InlineKeyboardButton(
                text=f"🔐 Права ({len(sub.allowed_commands)})",
                callback_data=_cb(commands.GUEST_PERMS_CODE, sub.chat_id, 0),
            )
        ],
        [
            InlineKeyboardButton(
                text="📊 Статистика VPN",
                callback_data=_cb(commands.GUEST_STATS_CODE, sub.chat_id),
            )
        ],
        [
            InlineKeyboardButton(
                text="🏠 Исключить из семьи" if sub.family else "🏠 Сделать членом семьи",
                callback_data=_cb(commands.GUEST_FAMILY_CODE, sub.chat_id),
            )
        ],
        [
            InlineKeyboardButton(
                text="🚪 Выставить",
                callback_data=_cb(commands.GUEST_KICK_CONFIRM_CODE, sub.chat_id),
            )
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.GUESTS_LIST_CODE, 0))],
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
                text="✅ Да, выставить", callback_data=_cb(commands.GUEST_REVOKE_CODE, sub.chat_id)
            )
        ],
        [
            InlineKeyboardButton(
                text="⬅️ Отмена", callback_data=_cb(commands.GUEST_CARD_CODE, sub.chat_id)
            )
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def build_back_to_card_view(
    sub_name: str, chat_id: int, body: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Общий каркас «текст + кнопка назад к карточке» (для VPN-статистики)."""
    text = f"📊 <b>{escape(sub_name)}</b>\n\n{body}"
    buttons = [
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=_cb(commands.GUEST_CARD_CODE, chat_id))]
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


# --- права гостя -------------------------------------------------------


def _granted_entries(sub: Subscription) -> list[tuple[str, str, str]]:
    """Что выдано гостю, строками экрана: (подпись, код снятия, аргумент).

    Права одной службы схлопываются в одну строку-группу (guest_rights.py) —
    иначе VPN занимает семь строк из шести на странице. Частично выданная
    группа показывает дробь: владелец видит, что комплект неполный, но снимает
    его всё равно целиком.
    """
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
    """Сколько строк на экране прав — по ним считается пагинация, а не по
    числу самих прав: группа занимает одну строку на всю службу."""
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
                callback_data=_cb(code, sub.chat_id, offset, arg),
            )
        ]
        for text, code, arg in page
    ]
    nav = nav_row(
        offset,
        PERM_PAGE_SIZE,
        len(entries),
        lambda o: _cb(commands.GUEST_PERMS_CODE, sub.chat_id, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="➕ Добавить право",
                callback_data=_cb(commands.GUEST_PERM_ADD_LIST_CODE, sub.chat_id, 0),
            )
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=_cb(commands.GUEST_CARD_CODE, sub.chat_id)
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


def _addable_entries(sub: Subscription) -> list[tuple[str, str, str, str]]:
    """Что ещё можно выдать: (подпись, код выдачи, аргумент, подсказка).

    Группа предлагается, пока выдана не полностью — дожать неполный комплект
    той же кнопкой проще, чем искать недостающие права по одному.
    """
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
                callback_data=_cb(code, sub.chat_id, offset, arg),
            )
        ]
        for text, code, arg, _note in page
    ]
    nav = nav_row(
        offset,
        PERM_PAGE_SIZE,
        len(addable),
        lambda o: _cb(commands.GUEST_PERM_ADD_LIST_CODE, sub.chat_id, o),
    )
    if nav:
        buttons.append(nav)
    buttons.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад", callback_data=_cb(commands.GUEST_PERMS_CODE, sub.chat_id, 0)
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)
