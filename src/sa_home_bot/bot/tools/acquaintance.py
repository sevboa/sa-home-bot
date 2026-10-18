from __future__ import annotations

from typing import Any

from sa_home_bot.bot import (
    recipients,
)
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec, people_of
from sa_home_bot.bot.tools.delivery import NEED_RECIPIENT_ID, parse_recipient_id

RELATION_ACQUAINTANCE = "acquaintance"


def _guest_facing_name(ctx: ToolContext, chat_id: int, fallback: str) -> str:
    return people_of(ctx).display(chat_id) or fallback


async def are_acquainted(store: Any, a: int, b: int) -> bool:
    return any({r["guest_a"], r["guest_b"]} == {a, b} for r in await store.relationships_for(a))


async def acquaintance_conflict(
    store: Any,
    initiator: int,
    target: int,
    target_name: str,
    *,
    ignore_action_id: int | None = None,
) -> str | None:
    pair = {initiator, target}
    for row in await store.open_pending_actions("relationship", initiator):
        if row["id"] == ignore_action_id or {row["initiator"], row["addressee"]} != pair:
            continue
        if row["initiator"] != initiator:
            return (
                f"адресат ({target_name}) уже сам предложил вам знакомство — "
                "ответьте кнопкой «Принять» в его форме"
            )
        if row["status"] == "draft":
            return (
                f"форма предложения знакомства адресату ({target_name}) уже открыта — "
                "ждёт кнопки «Отправить» или «Отмена»"
            )
        return (
            f"предложение знакомства уже отправлено адресату ({target_name}), ждём "
            f"ответа. Если собеседник говорил не про {target_name} — id не тот: "
            "найди нужного find_person и не говори, что ему уже отправлено"
        )
    if await are_acquainted(store, initiator, target):
        return f"вы с адресатом ({target_name}) уже знакомы"
    return None


async def tool_request_acquaintance(ctx: ToolContext, args: dict[str, Any]) -> str:
    if (
        ctx.chat_id is None
        or ctx.book is None
        or ctx.store is None
        or ctx.pending_actions is None
    ):
        return "недоступно: форму знакомства можно открыть только в живом разговоре"
    recipient_id = parse_recipient_id(args)
    if recipient_id is None:
        return NEED_RECIPIENT_ID.replace("find_person", 'find_person(purpose="acquaintance")')
    found = recipients.find_by_chat_id(recipient_id, ctx.book, people_of(ctx))
    if not found:
        return (
            f"не получилось: id {recipient_id} — ни один гость. id не придумывай: "
            'вызови find_person(purpose="acquaintance") с тем, как собеседник '
            "назвал человека, и возьми id из ответа"
        )
    target_chat_id = found[0].chat_id
    if target_chat_id == ctx.chat_id:
        return "ошибка: нельзя предложить знакомство самому себе"
    proposer = ctx.book.for_chat(ctx.chat_id)
    target = ctx.book.for_chat(target_chat_id)
    if proposer is None or target is None:
        return "недоступно: знакомиться через меня могут только приглашённые гости"
    proposer_name = _guest_facing_name(ctx, ctx.chat_id, proposer.name)
    target_name = _guest_facing_name(ctx, target_chat_id, target.name)

    conflict = await acquaintance_conflict(ctx.store, ctx.chat_id, target_chat_id, target_name)
    if conflict is not None:
        return conflict
    await ctx.pending_actions.create_relationship_draft(
        ctx.chat_id, target_chat_id, RELATION_ACQUAINTANCE, proposer_name, target_name
    )
    return (
        "Форма открыта, адресату пока НИЧЕГО не отправлено. Сразу после твоего "
        "ответа собеседник получит отдельное сообщение-форму: предложение "
        f"знакомства, адресат — {target_name}, кнопки «Отправить» и «Отмена», "
        "форма действует 1 час. Собеседник в этом чате — сам инициатор; "
        "обращайся к нему на «вы» и не называй его по имени в третьем лице. "
        "Коротко скажи своими словами, что форма ниже и что отправит "
        "предложение он сам кнопкой, а после согласия адресата ты сможешь "
        "передавать сообщения между ними. Не говори, что уже отправил, и не "
        "проси подтвердить текстом."
    )


async def tool_my_acquaintances(ctx: ToolContext, _args: dict[str, Any]) -> str:
    if ctx.chat_id is None or ctx.book is None or ctx.store is None:
        return "недоступно: сейчас не вижу своих знакомых"
    me = ctx.book.for_chat(ctx.chat_id)
    if me is None:
        return "недоступно: тебя нет в списке гостей"

    roster = await acquaintance_roster(ctx)
    if not roster:
        return "подтверждённых знакомств нет"
    lines = [f"🤝 {name} — id {chat_id}" for chat_id, name in roster]
    return (
        "Знакомы (могу передавать сообщения; в tell укажи recipient_id):\n"
        + "\n".join(lines)
    )


async def acquaintance_roster(ctx: ToolContext) -> list[tuple[int, str]]:
    if ctx.chat_id is None or ctx.book is None or ctx.store is None:
        return []
    roster: list[tuple[int, str]] = []
    for row in await ctx.store.relationships_for(ctx.chat_id, status="confirmed"):
        other_chat_id = row["guest_b"] if row["guest_a"] == ctx.chat_id else row["guest_a"]
        other = ctx.book.for_chat(other_chat_id)
        name = (other.invited_user or other.name) if other is not None else "?"
        roster.append((other_chat_id, name))
    return roster


async def roster_hint(ctx: ToolContext) -> str:
    roster = await acquaintance_roster(ctx)
    if not roster:
        return " Подтверждённых знакомых у собеседника нет."
    names = ", ".join(f"{name} (id {chat_id})" for chat_id, name in roster)
    return (
        f" Знакомые собеседника: {names}. Если имелся в виду кто-то из них — "
        "вызови tell ещё раз с его recipient_id; если неясно кто — "
        "переспроси у собеседника, не угадывай."
    )


_DECL_REQUEST_ACQUAINTANCE: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_acquaintance",
        "description": (
            "Открыть собеседнику форму предложения знакомства с другим гостем. "
            "Знакомство — единственная связь между гостями: после согласия "
            "обеих сторон ты сможешь передавать сообщения между ними (tell). "
            "Вызывай, когда собеседник говорит о своих отношениях с кем-то "
            "('Вася — мой друг', 'Настя — моя сестра', 'это моя жена', "
            "'я знаю Олега, это мой отец', 'познакомь меня с Игорем', "
            "'установи между нами связь') или хочет, чтобы ты передавал "
            "сообщения человеку, а tell отказал. Не переспрашивай и не "
            "предлагай словами — сразу вызывай. Какие бы слова об отношениях "
            "он ни выбрал — предлагай именно знакомство, не спорь о словах и "
            "не уточняй степень близости: система хранит только сам факт "
            "знакомства. Тул ничего не отправляет адресату: собеседник "
            "получит отдельную форму с кнопками «Отправить»/«Отмена» и решит "
            "сам. Ответ адресата тоже приходит только кнопкой — сам ты "
            "предложения не отправляешь, не принимаешь и не отклоняешь. "
            "Адресат — по recipient_id: найди его через "
            "find_person(purpose=\"acquaintance\"). Если "
            "тул вернул отказ — перескажи причину из его ответа как есть."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_id": {
                    "type": "integer",
                    "description": "Telegram id человека из find_person(purpose=\"acquaintance\")",
                },
            },
            "required": ["recipient_id"],
        },
    },
}

_DECL_MY_ACQUAINTANCES: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "my_acquaintances",
        "description": (
            "Узнать, с кем из гостей у собеседника подтверждённое знакомство "
            "(им ты можешь передавать сообщения) — используй, когда "
            "спрашивают о своих связях/знакомых/друзьях ('с кем я связан', "
            "'кому ты можешь от меня передать'). Про предложения без ответа "
            "или отклонённые тул молчит — не спойлери их, если спросят "
            "прямо, отвечай только тем, что тут вернулось."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


REQUEST_ACQUAINTANCE = ToolSpec(
    name="request_acquaintance",
    handler=tool_request_acquaintance,
    declaration=_DECL_REQUEST_ACQUAINTANCE,
)

MY_ACQUAINTANCES = ToolSpec(
    name="my_acquaintances",
    handler=tool_my_acquaintances,
    declaration=_DECL_MY_ACQUAINTANCES,
)
