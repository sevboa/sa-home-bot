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
HELP_COMMAND = f"/{commands.HELP.name}"
# Дописывается, если модель всё-таки не упомянула /help сама (см. ниже).
FALLBACK_HINT = f"<i>Полный список команд — {HELP_COMMAND}</i>"
NO_INVITES_TEXT = (
    "⚠️ Приглашения выключены: боту некуда записать гостя (нет пакета инстанса). "
    "Запустите бота с <code>--instance</code>, тогда гостевые подписки будут "
    "жить рядом с основным пакетом и реплицироваться по рою."
)
# Директива приветствия. Как и OPENING_PROMPT в bot/handlers/ai.py, в историю
# диалога не пишется — только ответ на неё.
#
# Решение пользователя 2026-07-30: встречает гостя ИМЕННО модель, а не
# заготовленный текст, и заведомо небыстрый путь (разбудить машину, прогреть
# модель — со «шагами» и Агнольдом, см. ai_flow) здесь не недостаток, а
# интрига: первое впечатление важнее пары секунд. Единственное жёсткое
# требование к содержанию — подсказка про /help в конце: без неё гость не
# узнает, что ещё умеет бот, а меню команд у него появляется молча.
WELCOME_PROMPT = (
    "Тебя только что познакомили с новым собеседником: его впустили в дом по "
    "одноразовому приглашению, и это его первые секунды здесь. Поздоровайся в "
    "характере, представься и дай понять, что к тебе можно обращаться просто "
    "словами, без команд. ОБЯЗАТЕЛЬНО закончи сообщение отдельной строкой с "
    "подсказкой, что полный список доступных команд открывается по /help — "
    "именно так, с косой чертой, чтобы её можно было нажать."
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


class CodeFromInsider(Filter):
    """Код приглашения, присланный в чат, который и так подписной.

    Живая находка 2026-07-30 (первый живой прогон): человек, у которого
    подписка уже была, прислал код боту — гейт пропустил сообщение как
    обычный текст, и оно уехало в разговор с Альфредом, а тот принял код за
    поисковый запрос и отправил его в веб-поиск. То есть одноразовый секрет
    ушёл во внешний поисковик, а человек так и не понял, почему «инвайт не
    работает». Перехватываем такое сообщение до всех широких фильтров.
    """

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
    """Объяснить, что код тут не нужен, и не пустить его дальше в разговор."""
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
        # Код мог уйти не по адресу — тот, кто его выпускал, должен знать и
        # решить, отзывать ли (/guests).
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
) -> None:
    """Первое, что видит впущенный, — живого Альфреда, а не системный текст.

    Решение пользователя 2026-07-30: встречает гостя модель. Дорога к ней
    бывает долгой (машину надо разбудить, модель — прогреть), и по пути гость
    увидит «шаги» и Агнольда из обычного presence-сценария (bot/ai_flow.py) —
    это оставлено намеренно, как интрига: расположить к себе важнее, чем
    ответить за секунду.

    Системное «приглашение принято» со списком команд осталось ровно на один
    случай — Альфреда не нашли вовсе: молча впустить и ничего не сказать
    нельзя, человек не поймёт, сработал ли код.
    """
    subscription = admission.subscription
    app_actions = await apps_link.actions()
    # Меню чата — молча: список команд гость увидит по /help, о котором ему
    # скажет сам Альфред.
    await refresh_chat_menu(bot, message.chat.id, subscription, app_actions)

    if admission.invited_by_chat_id and admission.invited_by_chat_id != message.chat.id:
        await notifier.send_direct(
            admission.invited_by_chat_id,
            "✅ Приглашение использовано: "
            f"{escape(subscription.name)} (<code>{message.chat.id}</code>).",
        )

    right = commands.required_right(commands.ALFRED.name)
    greeting = None
    if subscription.allows_command(right):
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
        )
    if greeting is None:
        await message.answer(WELCOME_TEXT + "\n\n" + build_help(subscription, app_actions))
        return
    if HELP_COMMAND not in greeting:
        # Промпт просит модель закончить подсказкой про /help, но обещание
        # модели — не гарантия (та же природа, что у живой находки про
        # get_time: маленькая модель выполняет инструкцию не всегда). Раз
        # подсказка объявлена обязательной, дописываем её сами — одной
        # строкой, чтобы не спорить с только что сказанным.
        await message.answer(FALLBACK_HINT)


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
