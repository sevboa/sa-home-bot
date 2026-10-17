from __future__ import annotations

from html import escape
from typing import Any

from sa_home_bot.bot import (
    recipients,
)
from sa_home_bot.bot.tools.acquaintance import are_acquainted, roster_hint
from sa_home_bot.bot.tools.core import CommandRight, ToolContext, ToolSpec, text_arg
from sa_home_bot.bot.tools.delivery import (
    NEED_RECIPIENT_ID,
    deliver_personal_message,
    parse_recipient_id,
)

TELL_RIGHT = "tell@llm"


def render_tell(text: str, author: str | None, to_owner_role: bool = False) -> str:
    """Как выглядит доставленное сообщение.

    Отдельная «шапка» обязательна: человек должен видеть, что это не бот сам
    придумал написать и не сообщение от системы, а Альфред передаёт просьбу
    конкретного человека. Текст — от Альфреда и в его манере, поэтому идёт
    как обычная его реплика.

    ``to_owner_role`` — гость назвал не личное имя владельца, а его роль
    («хозяин»/«владелец»/«админ», см. recipients.SOURCE_OWNER_ROLE): владелец
    должен сразу увидеть, что к нему обратились официально, а не просто
    написали лично ему как знакомому.
    """
    role = " (к вам как к владельцу)" if to_owner_role else ""
    who = f" по просьбе {escape(author)}" if author else ""
    return f"📨 <b>Альфред{role}{who}:</b>\n\n{escape(text.strip())}"


async def tool_tell(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.book is None or (ctx.notifier is None and ctx.emit is None):
        return "недоступно: сейчас я не могу никому написать"
    if ctx.chat_id is None:
        return "недоступно: непонятно, от кого передавать"
    recipient_id = parse_recipient_id(args)
    text = text_arg(args, "text")
    if recipient_id is None:
        return NEED_RECIPIENT_ID
    if not text:
        return "ошибка: не сказано, что передать (text)"

    # Владелец по РОЛИ ("передай хозяину/владельцу/админу") доступен всем
    # всегда. По личному имени ("передай Алексею") владелец — такой же
    # человек, как все: нужно знакомство (решение 2026-09-27).
    def _to_owner_role(target: recipients.Recipient) -> bool:
        return target.source == recipients.SOURCE_OWNER_ROLE

    async def guard(target: recipients.Recipient) -> str | None:
        if _to_owner_role(target):
            return None
        if ctx.store is None and ctx.notifier is None:
            # Служба tasks: Store нет — знакомство проверит бот перед
            # отправкой (emit_extra ниже → require_acquaintance).
            return None
        if ctx.store is not None and await are_acquainted(ctx.store, ctx.chat_id, target.chat_id):
            return None
        owner_hint = (
            " (официальное уведомление от владельца — notify_guest)"
            if ctx.subscription is not None and ctx.subscription.is_owner
            else ""
        )
        return (
            f"не умею: вы с {target.display} ещё не знакомы через меня — лично передавать "
            "сообщения я могу только тем, с кем знакомство подтверждено (и владельцу, "
            "если просят передать именно «владельцу»/«хозяину»); познакомить — "
            f"request_acquaintance(recipient_id={target.chat_id}){owner_hint}."
        ) + await roster_hint(ctx)

    def render(target: recipients.Recipient) -> str:
        return render_tell(
            text, ctx.author, to_owner_role=target.source == recipients.SOURCE_OWNER_ROLE
        )

    def emit_extra(target: recipients.Recipient) -> dict[str, Any]:
        if _to_owner_role(target):
            return {}
        return {"require_acquaintance": [ctx.chat_id, target.chat_id]}

    return await deliver_personal_message(
        ctx,
        recipient_id,
        text,
        render,
        guard=guard,
        emit_extra=emit_extra,
        hint=lambda: roster_hint(ctx),
        to_owner_role=bool(args.get("to_owner_role")),
    )


_DECL_TELL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "tell",
        "description": (
            "Передать личное сообщение другому человеку в его личный чат с "
            "тобой. Используй, когда собеседник просит что-то кому-то "
            "сообщить, передать, спросить или напомнить ('скажи Андрею, что…', "
            "'спроси у Наташи…'). Адресат — ТОЛЬКО по recipient_id: возьми его "
            "из списка знакомых в справке или вызови find_person с тем, как "
            "собеседник описал человека (имя, прозвище, «мама», «хозяину»). "
            "Если find_person дал несколько кандидатов — переспроси, не выбирай "
            "сам. Текст сообщения придумываешь ТЫ: перескажи просьбу своими "
            "словами, в своей манере, и упомяни, от кого она — это не пересылка "
            "дословной цитаты. Писать можно тем, с кем у собеседника "
            "подтверждено знакомство (request_acquaintance), и владельцу, если "
            "просят передать именно «владельцу»/«хозяину»/«админу» — тогда "
            "to_owner_role=true (find_person так и подскажет); по личному имени "
            "владелец — такой же человек, как все. tell — это ВСЕГДА личная "
            "передача (с пометкой «по просьбе X»), не официальное уведомление — "
            "для того есть notify_guest (виден только владельцу). Если с тобой "
            "говорит владелец и из его слов неясно, хочет ли он передать что-то "
            "лично от себя (как Алексей, tell) или объявить официально (как "
            "владелец, notify_guest) — спроси прямо: «сказать как лично от тебя "
            "или как официальное уведомление?», не выбирай сам. Если тул вернул "
            "отказ — перескажи ПРИЧИНУ ИЗ ЕГО ОТВЕТА как есть, не выдумывай "
            "другую от себя. Никогда не отказывай, не вызвав tell: знакомство "
            "могло появиться с прошлого раза — проверяет только тул."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "integer",
                    "description": "Telegram id получателя — из find_person или списка знакомых",
                },
                "text": {
                    "type": "string",
                    "description": "Готовый текст сообщения — то, что получатель прочтёт",
                },
                "to_owner_role": {
                    "type": "boolean",
                    "description": "true, если передают «владельцу/хозяину/админу» как роли",
                },
            },
            "required": ["recipient_id", "text"],
        },
    },
}


TELL = ToolSpec(
    name="tell",
    handler=tool_tell,
    declaration=_DECL_TELL,
    requires=CommandRight(TELL_RIGHT),
    telegram_only=True,
)
