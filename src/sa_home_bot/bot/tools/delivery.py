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


# Живая находка 2026-08-06: раньше был один лимит на автора (10/час) —
# рассылка ОДНОГО notify_guest десятерым разным гостям тратила его целиком
# так же, как повторная отправка ОДНОМУ И ТОМУ ЖЕ человеку 10 раз подряд,
# хотя риски у этих сценариев разные (первое — легитимная рассылка
# владельца, второе — модель зациклилась и спамит). Решение пользователя:
# два независимых потолка — сколько получает один конкретный человек и
# сколько всего уходит из одного чата-инициатора.
TELL_MAX_PER_RECIPIENT_PER_HOUR = 10  # одному и тому же адресату
TELL_MAX_TOTAL_PER_HOUR = 100  # всего от одного чата-инициатора (рассылки)
_tell_limiter = invites.AttemptLimiter(TELL_MAX_PER_RECIPIENT_PER_HOUR)  # ключ: (кто просил, кому)
_tell_broadcast_limiter = invites.AttemptLimiter(TELL_MAX_TOTAL_PER_HOUR)  # ключ: кто просил


def parse_recipient_id(args: dict[str, Any]) -> int | None:
    """recipient_id из аргументов тула: число, строка с числом или «id 123»."""
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
    """Общая доставка личного сообщения — поиск получателя по id, лимит,
    отправка, запись хода диалога. Права (если нужны) проверяет ``guard``:
    он получает найденного получателя и либо разрешает (``None``), либо
    возвращает готовый отказ. Без ``guard`` доставка разрешена всем, кто
    вообще видит вызывающий тул — так и должно быть у admin-only тулов,
    видимость которых уже гейтится декларацией (bot/tools.py:195-199).

    Этап 54.5: только по id. Раньше адресата искали по имени, которое
    придумала модель, и tell часто отвечал «такого нет» (14 неудач к
    2026-10-08). Теперь id находит find_person, а здесь он лишь проверяется.

    ``to_owner_role`` — гость назвал не имя владельца, а его роль; id при
    этом обязан быть владельцем, иначе отказ.

    ``allow_self`` — отправка самому себе (тот же chat_id, что у ctx). Для
    tell это бессмысленно (собеседник и так читает этот же чат — см. "не
    нужно: это тот же чат"), а вот у notify_guest смысл есть: владелец может
    попросить стилизованное уведомление себе самому — например, как
    self-напоминание через remind ("напомни мне как граф, что пора спать").

    ``emit_extra`` — доп. поля события доставки через мост службы tasks:
    проверки, которые там сделать нечем (нет Store), бот доделает сам перед
    отправкой (bot/node_events.py::_handle_deliver_message).

    ``hint`` — дописка к «не знаю такого»: кому писать МОЖНО (у tell — список
    знакомых с id), чтобы модель повторила вызов точно, а не угадывала.
    """
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

    # Порядок важен: сперва более тесный потолок (один получатель), чтобы
    # зацикленная на одном человеке модель не жгла попусту общий бюджет
    # рассылки — тогда отказ по нему не мешает рассылке другим.
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
        # Записываем как свой ход в диалоге получателя: тогда он ответит
        # обычным реплаем и разговор продолжится (реплай-цепочка резолвится
        # по ai_turns, см. bot/handlers/ai.py::AiReplyContinuation), а не
        # упрётся в сообщение, на которое некому отвечать.
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

    # Служба tasks (self-scheduled remind, живая находка 2026-08-06): своего
    # notifier нет, но есть мост к боту — единственному, у кого он есть (см.
    # ToolContext.emit). Доставка fire-and-forget: bot/node_events.py::
    # _handle_deliver_message шлёт сообщение и сам пишет ai_turn, здесь
    # подтверждения ждать неоткуда (тот же компромисс, что у task_result).
    assert ctx.emit is not None  # гарантировано вызывающим (tool_tell/tool_notify_guest)
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
