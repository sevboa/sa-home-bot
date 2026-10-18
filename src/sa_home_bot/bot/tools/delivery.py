from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sa_home_bot.bot import (
    invites,
    recipients,
)
from sa_home_bot.bot.tools.core import ToolContext, people_of
from sa_home_bot.tasks import protocol as task_protocol
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)


TELL_MAX_PER_RECIPIENT_PER_HOUR = 10
TELL_MAX_TOTAL_PER_HOUR = 100
_tell_limiter = invites.AttemptLimiter(TELL_MAX_PER_RECIPIENT_PER_HOUR)
_tell_broadcast_limiter = invites.AttemptLimiter(TELL_MAX_TOTAL_PER_HOUR)


def parse_recipient_id(args: dict[str, Any]) -> int | None:
    raw = args.get("recipient_id")
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    return recipients.query_chat_id(text)


NEED_RECIPIENT_ID = (
    "ошибка: адресат указывается только по id (recipient_id). Сначала вызови "
    "find_person с тем, как собеседник описал человека, и возьми id оттуда; "
    "если подходят несколько — переспроси собеседника."
)


def _got_verb(gender: str | None, *, future: bool = False) -> str:
    if future:
        return "получит"
    return {"m": "получил", "f": "получила"}.get(gender or "", "получил(а)")


async def deliver_personal_message(
    ctx: ToolContext,
    recipient_id: int,
    text: str,
    render: Callable[[recipients.Recipient], str],
    guard: Callable[[recipients.Recipient], Awaitable[str | None]] | None = None,
    allow_self: bool = False,
    emit_extra: Callable[[recipients.Recipient], dict[str, Any]] | None = None,
    hint: Callable[[], Awaitable[str]] | None = None,
    to_owner_role: bool = False,
) -> str:
    found = recipients.find_by_chat_id(recipient_id, ctx.book, people_of(ctx))
    if not found:
        return (
            f"не получилось: id {recipient_id} — ни один гость. id не придумывай: "
            "вызови find_person с тем, как собеседник назвал человека, и возьми id "
            "из ответа."
        ) + (await hint() if hint is not None else "")
    target = found[0]
    if to_owner_role:
        sub = ctx.book.for_chat(target.chat_id)
        if sub is None or not sub.is_owner:
            return (
                f"ошибка: to_owner_role только для владельца, а id {recipient_id} — "
                f"{target.display}"
            )
        target = recipients.Recipient(
            target.chat_id, target.display, recipients.SOURCE_OWNER_ROLE
        )
    if not allow_self and target.chat_id == ctx.chat_id:
        return "не нужно: это тот же чат, просто скажи это здесь"

    if guard is not None:
        refusal = await guard(target)
        if refusal is not None:
            return refusal

    if not _tell_limiter.register((ctx.chat_id, target.chat_id)):
        return f"не сейчас: {target.display} уже получил слишком много сообщений за последний час"
    if not _tell_broadcast_limiter.register(ctx.chat_id):
        return (
            "не сейчас: слишком много сообщений отправлено за последний час — "
            "общий лимит рассылки"
        )

    rendered = render(target)
    if ctx.notifier is not None:
        message_id = await ctx.notifier.send_direct(target.chat_id, rendered)
        if message_id is None:
            return f"не дошло: {target.display} сейчас недоступен"
        if ctx.store is not None:
            await ctx.store.record_ai_turn(
                target.chat_id, message_id, message_id, "assistant", text, utc_now()
            )
        log.info(
            "tell: сообщение от chat=%s доставлено chat=%s (%s)",
            ctx.chat_id, target.chat_id, target.display,
        )
        verb = _got_verb(people_of(ctx).gender(target.chat_id))
        return f"передано: {target.display} {verb} сообщение"

    assert ctx.emit is not None
    await ctx.emit(
        task_protocol.EVENT_DELIVER_MESSAGE,
        {
            "chat_id": target.chat_id,
            "html": rendered,
            "plain": text,
            "message_thread_id": None,
            **(emit_extra(target) if emit_extra is not None else {}),
        },
    )
    log.info(
        "tell: сообщение от chat=%s поставлено на доставку в chat=%s (%s) через tasks",
        ctx.chat_id, target.chat_id, target.display,
    )
    return f"передано: {target.display} получит сообщение"
