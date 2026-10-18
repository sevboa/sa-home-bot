from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, Message

from sa_home_bot.bot import ai_flow, commands, guest_rights, guests_view, invites
from sa_home_bot.bot.away import KIND_SUMMON, AwayService
from sa_home_bot.bot.handlers import ai as ai_handlers
from sa_home_bot.bot.handlers import vpn as vpn_handlers
from sa_home_bot.bot.handlers.basic import build_help
from sa_home_bot.bot.interactives.engine import Interactives
from sa_home_bot.bot.invites import Admission, Gatekeeper
from sa_home_bot.bot.menu import refresh_chat_menu
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.bot.ui.callback_data import int_or
from sa_home_bot.bot.ui.redraw import redraw
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="invites")

WELCOME_TEXT = "🔑 Приглашение принято — добро пожаловать."
MENU_HINT_KEYWORD = "меню"
FALLBACK_HINT = "<i>Полный список умений — в меню команд (значок рядом с полем ввода)</i>"
NO_INVITES_TEXT = (
    "⚠️ Приглашения выключены: боту некуда записать гостя (нет пакета инстанса). "
    "Запустите бота с <code>--instance</code>, тогда гостевые подписки будут "
    "жить рядом с основным пакетом и реплицироваться по рою."
)
MAX_INVITE_HOURS = 720.0
BAD_INVITE_HOURS_TEXT = (
    f"ошибка: часы должны быть числом больше 0 и не больше {MAX_INVITE_HOURS:g} "
    "(пример: <code>/invite 3</code>)"
)
WELCOME_PROMPT = (
    "Тебя только что познакомили с новым собеседником: его впустили в дом по "
    "одноразовому приглашению, и это его первые секунды здесь. Поздоровайся в "
    "характере, представься и дай понять, что к тебе можно обращаться просто "
    "словами, без команд. ОБЯЗАТЕЛЬНО закончи сообщение отдельной строкой с "
    "подсказкой, что полный список доступных умений можно увидеть в меню "
    "команд (значок рядом с полем ввода сообщения)."
)


class JustAdmitted(Filter):
    async def __call__(self, message: Message, admission: Admission | None = None) -> bool:
        return admission is not None


def _plural(n: int, one: str, few: str, many: str) -> str:
    tail = n % 100
    if 11 <= tail <= 14:
        return many
    if tail % 10 == 1:
        return one
    if tail % 10 in (2, 3, 4):
        return few
    return many


def _format_expiry(expires: datetime) -> str:
    left = expires - datetime.now(tz=UTC)
    total_minutes = max(-(-int(left.total_seconds()) // 60), 0)
    if total_minutes < 90:
        return f"{total_minutes} {_plural(total_minutes, 'минута', 'минуты', 'минут')}"
    total_hours = -(-total_minutes // 60)
    if total_hours < 48:
        return f"{total_hours} {_plural(total_hours, 'час', 'часа', 'часов')}"
    days = -(-total_hours // 24)
    return f"{days} {_plural(days, 'день', 'дня', 'дней')}"


def _parse_invite_hours(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    hours = float(raw.strip().replace(",", "."))
    if not (0 < hours <= MAX_INVITE_HOURS):
        raise ValueError(f"часы вне диапазона: {hours}")
    return hours


@router.message(Command(commands.INVITE.name))
async def cmd_invite(
    message: Message,
    command: CommandObject,
    gate: Gatekeeper,
    bot_username: str,
) -> None:
    if not gate.enabled:
        await message.answer(NO_INVITES_TEXT)
        return
    try:
        hours = _parse_invite_hours(command.args)
    except ValueError:
        await message.answer(BAD_INVITE_HOURS_TEXT)
        return
    sender = message.from_user
    code, expires = await gate.issue(
        message.chat.id,
        sender.id if sender else None,
        ttl_s=hours * 3600.0 if hours is not None else None,
    )
    link = f"https://t.me/{bot_username}?start={code}"
    await message.answer(
        "🔑 <b>Код приглашения</b>\n\n"
        f"<code>{invites.format_code(code)}</code>\n\n"
        f"{link}\n\n"
        "<i>Сообщите код Альфреду или воспользуйтесь ссылкой. "
        f"Инвайт доступен в течение {_format_expiry(expires)}.</i>"
    )


@router.message(Command(commands.GUESTS.name))
async def cmd_guests(message: Message, book: SubscriptionBook) -> None:
    guests = sorted(book.guests(), key=lambda g: g.invited_at)
    text, keyboard = guests_view.build_list_view(guests, 0)
    await message.answer(text, reply_markup=keyboard)


async def _guest_or_redraw_list(
    callback: CallbackQuery, book: SubscriptionBook, chat_id: int
) -> Subscription | None:
    sub = book.for_chat(chat_id)
    if sub is not None and sub.is_guest:
        return sub
    await callback.answer("Такого гостя уже нет", show_alert=True)
    guests = sorted(book.guests(), key=lambda g: g.invited_at)
    text, keyboard = guests_view.build_list_view(guests, 0)
    await redraw(callback, text, keyboard)
    return None


@router.callback_query(F.data.startswith(f"{commands.CALLBACK_PREFIX}:g_"))
async def on_guest_screen(
    callback: CallbackQuery,
    gate: Gatekeeper,
    book: SubscriptionBook,
    node_link: ServiceLink,
    bot: Bot,
) -> None:
    parts = (callback.data or "").split(":")
    code = parts[1] if len(parts) > 1 else ""

    if code == commands.GUESTS_LIST_CODE:
        offset = int_or(parts[2] if len(parts) > 2 else None)
        guests = sorted(book.guests(), key=lambda g: g.invited_at)
        text, keyboard = guests_view.build_list_view(guests, offset)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.OPEN_CODES_LIST_CODE:
        offset = int_or(parts[2] if len(parts) > 2 else None)
        open_codes = await gate.open_codes()
        text, keyboard = guests_view.build_codes_view(open_codes, offset)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.CODE_REVOKE_CODE:
        raw_code = parts[2] if len(parts) > 2 else ""
        ok = await gate.revoke_code(raw_code)
        toast = "Код отозван" if ok else "Код уже погашен или отозван"
        await callback.answer(toast, show_alert=not ok)
        open_codes = await gate.open_codes()
        text, keyboard = guests_view.build_codes_view(open_codes, 0)
        await redraw(callback, text, keyboard)
        return

    if code == commands.GUEST_CARD_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        sub = await _guest_or_redraw_list(callback, book, chat_id)
        if sub is None:
            return
        text, keyboard = guests_view.build_card_view(sub)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.GUEST_KICK_CONFIRM_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        sub = await _guest_or_redraw_list(callback, book, chat_id)
        if sub is None:
            return
        text, keyboard = guests_view.build_kick_confirm_view(sub)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.GUEST_REVOKE_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        ok = gate.revoke_guest(chat_id)
        if ok:
            await refresh_chat_menu(bot, chat_id, None)
            await callback.answer("Гость выставлен")
            if callback.message:
                await callback.message.answer(f"🚪 Гость <code>{chat_id}</code> больше не впущен.")
        else:
            await callback.answer("Такого гостя уже нет", show_alert=True)
        guests = sorted(book.guests(), key=lambda g: g.invited_at)
        text, keyboard = guests_view.build_list_view(guests, 0)
        await redraw(callback, text, keyboard)
        return

    if code == commands.GUEST_STATS_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        sub = await _guest_or_redraw_list(callback, book, chat_id)
        if sub is None:
            return
        body = await vpn_handlers.usage_text(node_link, chat_id)
        text, keyboard = guests_view.build_back_to_card_view(sub.name, chat_id, body)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.GUEST_PERMS_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        offset = int_or(parts[3] if len(parts) > 3 else None)
        sub = await _guest_or_redraw_list(callback, book, chat_id)
        if sub is None:
            return
        text, keyboard = guests_view.build_perms_view(sub, offset)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.GUEST_PERM_ADD_LIST_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        offset = int_or(parts[3] if len(parts) > 3 else None)
        sub = await _guest_or_redraw_list(callback, book, chat_id)
        if sub is None:
            return
        text, keyboard = guests_view.build_perm_add_view(sub, offset)
        await redraw(callback, text, keyboard)
        await callback.answer()
        return

    if code == commands.GUEST_PERM_OFF_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        offset = int_or(parts[3] if len(parts) > 3 else None)
        right = parts[4] if len(parts) > 4 else ""
        updated = gate.remove_guest_right(chat_id, right)
        if updated is None:
            await _guest_or_redraw_list(callback, book, chat_id)
            return
        await refresh_chat_menu(bot, chat_id, updated)
        await callback.answer("Право снято")
        await _redraw_perms(callback, updated, offset)
        return

    if code == commands.GUEST_PERM_ADD_CODE:
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        right = parts[4] if len(parts) > 4 else ""
        updated = gate.add_guest_right(chat_id, right)
        if updated is None:
            await _guest_or_redraw_list(callback, book, chat_id)
            return
        await refresh_chat_menu(bot, chat_id, updated)
        await callback.answer("Право выдано")
        text, keyboard = guests_view.build_perms_view(updated, 0)
        await redraw(callback, text, keyboard)
        return

    if code in (commands.GUEST_GROUP_ADD_CODE, commands.GUEST_GROUP_OFF_CODE):
        chat_id = int_or(parts[2] if len(parts) > 2 else None, default=-1)
        offset = int_or(parts[3] if len(parts) > 3 else None)
        group = guest_rights.group(parts[4] if len(parts) > 4 else "")
        sub = await _guest_or_redraw_list(callback, book, chat_id)
        if sub is None:
            return
        if group is None:
            await callback.answer()
            return
        adding = code == commands.GUEST_GROUP_ADD_CODE
        rights = (
            sub.allowed_commands | group.rights if adding else sub.allowed_commands - group.rights
        )
        updated = gate.set_guest_rights(chat_id, rights)
        if updated is None:
            await _guest_or_redraw_list(callback, book, chat_id)
            return
        await refresh_chat_menu(bot, chat_id, updated)
        await callback.answer(f"{group.label}: {'выдано' if adding else 'снято'}")
        await _redraw_perms(callback, updated, 0 if adding else offset)
        return

    await callback.answer()


async def _redraw_perms(callback: CallbackQuery, sub: Subscription, offset: int) -> None:
    offset = guests_view.clamp_offset(
        offset, guests_view.PERM_PAGE_SIZE, guests_view.granted_rows(sub)
    )
    text, keyboard = guests_view.build_perms_view(sub, offset)
    await redraw(callback, text, keyboard)


class CodeFromInsider(Filter):
    async def __call__(self, message: Message, gate: Gatekeeper) -> bool | dict:
        known = await gate.known_code(message.text)
        if known is None:
            return False
        code, row = known
        return {"insider_code": code, "insider_code_row": row}


@router.message(CodeFromInsider())
async def on_code_from_insider(
    message: Message,
    insider_code: str,
    insider_code_row: dict,
    notifier: Notifier,
) -> None:
    if insider_code_row.get("redeemed_at"):
        await message.answer("🔑 Этот код уже использован — второй раз он не сработает.")
        return
    if insider_code_row.get("revoked_at"):
        await message.answer("🔑 Этот код отозван.")
        return
    await message.answer(
        "🔑 Это код приглашения — вам он не нужен, вы и так у меня в списке.\n\n"
        "Код нужен тому, кого приглашаете: пусть он сам пришлёт его мне в личку "
        "или откроет ссылку из приглашения."
    )
    issuer = insider_code_row.get("issued_by_chat_id")
    if issuer and issuer != message.chat.id:
        await notifier.send_direct(
            issuer,
            f"ℹ️ Код <code>{invites.format_code(insider_code)}</code> прислали мне из чата "
            f"<code>{message.chat.id}</code>, который и так подписан — я объяснил, что он "
            "здесь не нужен. Если код ушёл не тому, отзовите его в /guests.",
        )


@router.message(JustAdmitted())
async def on_admitted(
    message: Message,
    admission: Admission,
    bot: Bot,
    apps_link: ServiceLink,
    node_link: ServiceLink,
    store: Store,
    config: Settings,
    book: SubscriptionBook,
    notifier: Notifier,
    active_ai_chats: ai_flow.ActiveAiChats,
    tool_calls: ToolCalls,
    away: AwayService | None = None,
    interactives: Interactives | None = None,
) -> None:
    subscription = admission.subscription
    app_actions = await apps_link.actions()
    await refresh_chat_menu(bot, message.chat.id, subscription, app_actions)

    if admission.invited_by_chat_id and admission.invited_by_chat_id != message.chat.id:
        await notifier.send_direct(
            admission.invited_by_chat_id,
            "✅ Приглашение использовано: "
            f"{escape(subscription.name)} (<code>{message.chat.id}</code>).",
        )

    right = commands.required_right(commands.ALFRED.name)
    greeting = None
    in_city = (
        subscription.allows_command(right)
        and away is not None
        and await away.intercept(message, kind=KIND_SUMMON)
    )
    if subscription.allows_command(right) and not in_city:
        greeting = await ai_handlers.start_dialogue(
            message,
            await _welcome_prompt(node_link, message, subscription),
            node_link=node_link,
            store=store,
            config=config,
            book=book,
            notifier=notifier,
            active_ai_chats=active_ai_chats,
            tool_calls=tool_calls,
            interactives=interactives,
        )
    if greeting is None:
        await message.answer(WELCOME_TEXT + "\n\n" + build_help(subscription, app_actions))
        return
    if MENU_HINT_KEYWORD not in greeting.lower():
        await message.answer(FALLBACK_HINT)


async def _welcome_prompt(
    node_link: ServiceLink, message: Message, subscription: Subscription
) -> str:
    query = (subscription.invited_user or subscription.name).split(" (@")[0]
    facts = await ai_flow.recall_facts(node_link, message.chat.id, query)
    if not facts:
        return WELCOME_PROMPT
    known = "; ".join(facts)
    return (
        WELCOME_PROMPT
        + " Вы уже знакомы — вот что ты о нём помнишь: "
        + known
        + ". Поздоровайся как со знакомым, без представлений заново."
    )
