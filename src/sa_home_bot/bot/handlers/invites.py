"""Приватный вход: /invite, /guests и встреча приглашённого.

Дверь (молчание для неподписных чатов) — в bot/middlewares.py::SilenceGate,
механика кода и гостевых подписок — в bot/invites.py. Здесь только то, что
видит человек: как выдаётся код, как выглядит список гостей и что происходит
в чате в момент, когда чужой становится своим.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from sa_home_bot.bot import ai_flow, commands, invites
from sa_home_bot.bot.handlers import ai as ai_handlers
from sa_home_bot.bot.handlers.basic import build_help
from sa_home_bot.bot.invites import Admission, Gatekeeper
from sa_home_bot.bot.menu import refresh_chat_menu
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.bot.tool_debug import ToolCalls
from sa_home_bot.config import Settings
from sa_home_bot.db.store import Store
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

router = Router(name="invites")

WELCOME_TEXT = "🔑 Приглашение принято — добро пожаловать."
NO_INVITES_TEXT = (
    "⚠️ Приглашения выключены: боту некуда записать гостя (нет пакета инстанса). "
    "Запустите бота с <code>--instance</code>, тогда гостевые подписки будут "
    "жить рядом с основным пакетом и реплицироваться по рою."
)
# Директива приветствия. Как и OPENING_PROMPT в bot/handlers/ai.py, в историю
# диалога не пишется — только ответ на неё.
WELCOME_PROMPT = (
    "Тебя только что познакомили с новым собеседником: его впустили по "
    "одноразовому приглашению. Поздоровайся коротко, в характере, представься "
    "и скажи, что тебя можно просто спрашивать."
)


class JustAdmitted(Filter):
    """Сообщение, которым чат предъявил годный код (SilenceGate его пометил)."""

    async def __call__(self, message: Message, admission: Admission | None = None) -> bool:
        return admission is not None


def _format_expiry(expires: datetime) -> str:
    left = expires - datetime.now(tz=UTC)
    minutes = max(int(left.total_seconds() // 60), 0)
    if minutes >= 60:
        return f"{minutes // 60} ч {minutes % 60} мин"
    return f"{minutes} мин"


@router.message(Command(commands.INVITE.name))
async def cmd_invite(
    message: Message,
    gate: Gatekeeper,
    bot_username: str,
) -> None:
    if not gate.enabled:
        await message.answer(NO_INVITES_TEXT)
        return
    sender = message.from_user
    code, expires = await gate.issue(message.chat.id, sender.id if sender else None)
    link = f"https://t.me/{bot_username}?start={code}"
    await message.answer(
        "🔑 <b>Код приглашения</b>\n\n"
        f"<code>{invites.format_code(code)}</code>\n\n"
        f"Ссылка (нажать и отправить /start): {link}\n\n"
        f"Годен {_format_expiry(expires)}, срабатывает один раз. "
        "В группе ссылка не поможет — там код нужно прислать сообщением.\n"
        "Гость получит право говорить со мной; остальное — по вашему решению."
    )


@router.message(Command(commands.GUESTS.name))
async def cmd_guests(message: Message, gate: Gatekeeper, book: SubscriptionBook) -> None:
    guests = sorted(book.guests(), key=lambda g: g.invited_at)
    open_codes = await gate.open_codes()

    lines = ["<b>Приглашённые</b>", ""]
    if guests:
        for guest in guests:
            lines.append(
                f"• {escape(guest.name)} — <code>{guest.chat_id}</code>"
                + (f", вошёл {guest.invited_at[:16].replace('T', ' ')}" if guest.invited_at else "")
            )
    else:
        lines.append("Пока никого.")
    if open_codes:
        lines += ["", "<b>Открытые коды</b>", ""]
        lines += [
            f"• <code>{invites.format_code(row['code'])}</code> — до "
            f"{row['expires_at'][:16].replace('T', ' ')}"
            for row in open_codes
        ]

    buttons = [
        [
            InlineKeyboardButton(
                text=f"Выставить {guest.name}"[:64],
                callback_data=f"{commands.CALLBACK_PREFIX}:"
                f"{commands.GUEST_REVOKE_CODE}:{guest.chat_id}",
            )
        ]
        for guest in guests
    ]
    buttons += [
        [
            InlineKeyboardButton(
                text=f"Отозвать код {invites.format_code(row['code'])}",
                callback_data=f"{commands.CALLBACK_PREFIX}:"
                f"{commands.CODE_REVOKE_CODE}:{row['code']}",
            )
        ]
        for row in open_codes
    ]
    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None,
    )


@router.callback_query(
    F.data.startswith(f"{commands.CALLBACK_PREFIX}:{commands.GUEST_REVOKE_CODE}:")
)
async def on_guest_revoke(callback: CallbackQuery, gate: Gatekeeper, bot: Bot) -> None:
    try:
        chat_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Не разобрал, кого выставлять", show_alert=True)
        return
    if not gate.revoke_guest(chat_id):
        await callback.answer("Такого гостя уже нет", show_alert=True)
        return
    # Меню чата больше не наше дело — убираем, чтобы у выставленного не
    # осталось кнопок, которые всё равно не сработают.
    await refresh_chat_menu(bot, chat_id, None)
    await callback.answer("Гость выставлен")
    if callback.message:
        await callback.message.answer(f"🚪 Гость <code>{chat_id}</code> больше не впущен.")


@router.callback_query(
    F.data.startswith(f"{commands.CALLBACK_PREFIX}:{commands.CODE_REVOKE_CODE}:")
)
async def on_code_revoke(callback: CallbackQuery, gate: Gatekeeper) -> None:
    code = (callback.data or "").rsplit(":", 1)[1]
    if not await gate.revoke_code(code):
        await callback.answer("Код уже погашен или отозван", show_alert=True)
        return
    await callback.answer("Код отозван")
    if callback.message:
        await callback.message.answer(
            f"🔒 Код <code>{invites.format_code(code)}</code> отозван."
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
) -> None:
    """Первое, что видит впущенный: факт входа, права — и живой Альфред.

    Порядок важен. Сначала уходит короткое системное сообщение: оно не
    зависит ни от какой службы, поэтому человек в любом случае понимает, что
    попал внутрь. Приветствие Альфреда — вторым: LLM-нода может спать или
    вовсе отсутствовать, и ждать её ради факта «вы вошли» незачем.
    """
    subscription = admission.subscription
    app_actions = await apps_link.actions()
    await refresh_chat_menu(bot, message.chat.id, subscription, app_actions)
    await message.answer(WELCOME_TEXT + "\n\n" + build_help(subscription, app_actions))

    if admission.invited_by_chat_id and admission.invited_by_chat_id != message.chat.id:
        await notifier.send_direct(
            admission.invited_by_chat_id,
            "✅ Приглашение использовано: "
            f"{escape(subscription.name)} (<code>{message.chat.id}</code>).",
        )

    right = commands.required_right(commands.ALFRED.name)
    if not subscription.allows_command(right):
        return
    await ai_handlers.start_dialogue(
        message,
        await _welcome_prompt(node_link, message, subscription),
        node_link=node_link,
        store=store,
        config=config,
        book=book,
        notifier=notifier,
        active_ai_chats=active_ai_chats,
        tool_calls=tool_calls,
    )


async def _welcome_prompt(
    node_link: ServiceLink, message: Message, subscription: Subscription
) -> str:
    """Директива приветствия, дополненная тем, что о человеке уже известно.

    Обычный путь подмешивания памяти (ai_flow.recall_facts внутри
    request_alfred) ищет по тексту текущей реплики — а здесь реплика это
    инвайт-код, по которому не найдётся ничего. Поэтому спрашиваем память
    сами и по осмысленному запросу: имени гостя.

    Память привязана к chat_id, и в личке chat_id равен user_id — поэтому
    человек, который когда-то уже говорил с Альфредом (а потом остался без
    подписки), будет узнан, а не встречен как незнакомец.
    """
    # Ищем по имени, без «(@username)»: память ищет по подстроке, и хвост со
    # скобками только сужает совпадения.
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
