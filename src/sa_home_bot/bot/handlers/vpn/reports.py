from __future__ import annotations

import html
import logging

from aiogram.filters import Filter
from aiogram.types import ForceReply, Message

from sa_home_bot.bot import (
    vpn_callbacks,
    vpn_faq,
    vpn_help,
    vpn_keyboards,
    vpn_nodes,
    vpn_report,
)
from sa_home_bot.bot.handlers.vpn.context import NOT_ALLOWED, Ctx
from sa_home_bot.bot.handlers.vpn.router import router
from sa_home_bot.bot.notifier import Notifier
from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.people.book import PeopleBook
from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

log = logging.getLogger(__name__)

_cb = vpn_callbacks


async def person(
    chat_id: int, user, subscription: Subscription | None, book, store
) -> tuple[str, str | None]:
    name = username = None
    pid = getattr(user, "id", None) or chat_id
    if store is not None:
        try:
            people = await PeopleBook.load(store, book)
            name, username = people.name(pid), people.username(pid) or None
        except Exception:
            log.warning("Карточка человека %s не прочиталась", pid, exc_info=True)
    name = name or getattr(user, "full_name", None) or (subscription.name if subscription else "")
    return name or str(chat_id), username or getattr(user, "username", None)


async def _report_context(node_link: ServiceLink, chat_id: int, device_key: str) -> list[str]:
    try:
        answered = await vpn_nodes.usage_for_chat(node_link, chat_id)
    except Exception:
        log.warning("Контекст заявки по VPN не собрался (chat=%s)", chat_id, exc_info=True)
        return []
    return vpn_help.context_or_empty(vpn_nodes.allowed_servers(answered), answered, device_key)


async def submit_report(
    node_link: ServiceLink,
    notifier: Notifier,
    book,
    store,
    *,
    chat_id: int,
    user,
    subscription: Subscription | None,
    reason: str,
    device_key: str = "",
) -> vpn_help.Outcome:
    blocked = vpn_help.blocked_outcome(book, chat_id)
    if blocked is not None:
        return blocked
    name, username = await person(chat_id, user, subscription, book, store)
    context = await _report_context(node_link, chat_id, device_key)
    return await vpn_help.send_help_request(
        book,
        notifier,
        chat_id=chat_id,
        who=vpn_help.who_text(name, username),
        reason=reason,
        context=context,
    )


def _menu_back(code: str, key: str) -> str:
    if key:
        return _cb.card_cb(_cb.SCREEN_DEVICE, key)
    if code == vpn_report.MENU_FAQ:
        return vpn_faq.list_cb()
    return _cb.home_cb()


async def handle_report(ctx: Ctx, value: str | None) -> None:
    callback = ctx.callback
    chat_id = ctx.chat_id
    code, key = vpn_report.parse_report(value)
    if _cb.is_probe(chat_id) or not vpn_report.can_report(ctx.subscription):
        await ctx.alert(NOT_ALLOWED)
        return
    if code in (vpn_report.MENU, vpn_report.MENU_FAQ):
        await callback.answer()
        await ctx.edit(
            vpn_report.MENU_TEXT,
            vpn_report.menu_keyboard(key, back_cb=_menu_back(code, key)),
        )
        return
    if vpn_help.is_throttled(chat_id):
        await ctx.alert(vpn_report.THROTTLED_TEXT)
        return
    user = getattr(callback, "from_user", None)
    if code == vpn_report.OTHER:
        await callback.answer()
        sent = await callback.message.answer(
            vpn_report.OTHER_PROMPT,
            reply_markup=ForceReply(input_field_placeholder="Что случилось?"),
        )
        _remember_prompt(
            chat_id,
            sent,
            vpn_report.new_pending(
                vpn_report.KIND_REPORT, getattr(user, "id", None) or chat_id, device_key=key
            ),
        )
        return
    outcome = await submit_report(
        ctx.node_link,
        ctx.notifier,
        ctx.book,
        ctx.store,
        chat_id=chat_id,
        user=user,
        subscription=ctx.subscription,
        reason=vpn_report.REASONS[code][1],
        device_key=key,
    )
    if outcome == "throttled":
        await ctx.alert(vpn_report.THROTTLED_TEXT)
        return
    await callback.answer()
    text = vpn_report.SENT_TEXT if outcome == "sent" else vpn_report.NOBODY_TEXT
    await ctx.edit(text, vpn_keyboards.done_keyboard())


def _remember_prompt(chat_id: int, sent: object, entry: vpn_report.Pending) -> None:
    message_id = getattr(sent, "message_id", None)
    if message_id is not None:
        vpn_report.remember(chat_id, message_id, entry)


async def handle_owner_reply_button(ctx: Ctx, value: str | None) -> None:
    callback = ctx.callback
    try:
        target = int(value or "")
    except ValueError:
        target = 0
    if not vpn_report.can_reply(ctx.subscription) or target == 0:
        await ctx.alert(NOT_ALLOWED)
        return
    await callback.answer()
    user = getattr(callback, "from_user", None)
    target_sub = ctx.book.for_chat(target) if ctx.book is not None else None
    name, username = await person(target, None, target_sub, ctx.book, ctx.store)
    shown = f"{name} (@{username})" if username else name
    sent = await callback.message.answer(
        html.escape(vpn_report.owner_prompt(shown)),
        reply_markup=ForceReply(input_field_placeholder="Ответ человеку"),
    )
    _remember_prompt(
        ctx.chat_id,
        sent,
        vpn_report.new_pending(
            vpn_report.KIND_ANSWER,
            getattr(user, "id", None) or ctx.chat_id,
            target_chat=target,
        ),
    )


class VpnReplyFilter(Filter):
    async def __call__(self, message: Message) -> bool | dict:
        reply = message.reply_to_message
        if (
            reply is None
            or message.chat is None
            or message.from_user is None
            or not message.text
            or message.text.startswith("/")
        ):
            return False
        entry = vpn_report.lookup(message.chat.id, reply.message_id)
        if entry is None or entry.user_id != message.from_user.id:
            return False
        return {"vpn_reply": entry}


@router.message(VpnReplyFilter())
async def on_vpn_reply(
    message: Message,
    vpn_reply: vpn_report.Pending,
    node_link: ServiceLink,
    notifier: Notifier,
    book: SubscriptionBook | None = None,
    store=None,
    subscription: Subscription | None = None,
) -> None:
    chat_id = message.chat.id
    reply_id = message.reply_to_message.message_id
    if vpn_report.is_expired(vpn_reply):
        vpn_report.forget(chat_id, reply_id)
        await message.answer(vpn_report.EXPIRED_TEXT)
        return
    text = (message.text or "").strip()[: vpn_report.MAX_DESCRIPTION]
    if subscription is None and book is not None:
        subscription = book.for_chat(chat_id)
    if vpn_reply.kind == vpn_report.KIND_ANSWER:
        if not vpn_report.can_reply(subscription):
            return
        vpn_report.forget(chat_id, reply_id)
        delivered = await notifier.send_direct(
            vpn_reply.target_chat, vpn_report.ANSWER_PREFIX + html.escape(text)
        )
        await message.answer(
            vpn_report.OWNER_SENT_TEXT if delivered is not None else vpn_report.OWNER_FAILED_TEXT
        )
        return
    if not vpn_report.can_report(subscription) or _cb.is_probe(chat_id):
        return
    if vpn_help.is_throttled(chat_id):
        await message.answer(vpn_report.THROTTLED_TEXT)
        return
    vpn_report.forget(chat_id, reply_id)
    outcome = await submit_report(
        node_link,
        notifier,
        book,
        store,
        chat_id=chat_id,
        user=message.from_user,
        subscription=subscription,
        reason=html.escape(text),
        device_key=vpn_reply.device_key,
    )
    if outcome == "throttled":
        await message.answer(vpn_report.THROTTLED_TEXT)
    else:
        await message.answer(vpn_report.SENT_TEXT if outcome == "sent" else vpn_report.NOBODY_TEXT)
