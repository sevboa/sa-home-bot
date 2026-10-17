from __future__ import annotations

from typing import Any

from sa_home_bot.bot import (
    recipients,
)
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec, people_of
from sa_home_bot.bot.tools.delivery import NEED_RECIPIENT_ID, parse_recipient_id

# --- знакомство между гостями (Этап 42.6 → 45 → 46) ---
#
# Решение пользователя 2026-09-27: связь между гостями ОДНА — факт
# знакомства, взаимный и без степеней. Типы друг/знакомый/родство/супруг(а)
# (42.6.x) убраны: выбор типа сталкивал лбами людей, не определившихся
# между собой, а «дружбу» каждый понимает по-своему. Что бы собеседник ни
# сказал об отношениях («мой друг», «моя жена»), Альфред предлагает
# установить знакомство. Групповой флаг «семья» (Subscription.family)
# убран тогда же: кто хочет переписываться через Альфреда — знакомы и
# запросят знакомство. Подтверждённое знакомство открывает tell в обе
# стороны (см. tool_tell). Запрос живёт в pending_actions и решается
# кнопками форм (bot/pending_actions.py), в guest_relationships пишется
# только итог (confirmed). Отзыв знакомства — пока не нужен (решение того
# же дня).
RELATION_ACQUAINTANCE = "acquaintance"


def _guest_facing_name(ctx: ToolContext, chat_id: int, fallback: str) -> str:
    """Имя для текста, который увидит ДРУГОЙ человек, не сам этот chat_id.

    Живая находка 2026-09-26: Subscription.name у владельца в проде технически
    "me" — это ключ подписки, не имя, а оно утекало в сообщения о связях
    ("Гость «me» предложил..."). Имя — из карточки человека (Этап 58).

    ВАЖНО (живая находка того же дня, второй заход): имя в карточке — в
    именительном падеже. НЕ вставлять результат прямо в предложение, где
    по-русски нужен другой падеж (дательный "кому", родительный "у кого",
    творительный "с кем" и т.п.) — ломается грамматика ("у Наташа Сорокина",
    "отправлено Наташа Сорокина"). Падеж держать на служебном слове
    ("адресату (…)", "с адресатом (…)"), а имя — именительным лейблом в
    скобках. И ни в каком падеже не называть по имени в третьем лице самого
    адресата текста (см. tool_request_acquaintance)."""
    return people_of(ctx).display(chat_id) or fallback


async def are_acquainted(store: Any, a: int, b: int) -> bool:
    """Подтверждённое знакомство пары — в любом направлении (guest_a/guest_b
    не упорядочены). Старые строки других типов (до 2026-09-27) миграция
    переписала в acquaintance, поэтому тип не сверяем."""
    return any({r["guest_a"], r["guest_b"]} == {a, b} for r in await store.relationships_for(a))


async def acquaintance_conflict(
    store: Any,
    initiator: int,
    target: int,
    target_name: str,
    *,
    ignore_action_id: int | None = None,
) -> str | None:
    """Почему знакомство сейчас нельзя предложить/принять — None, если
    можно. Проверяется на открытии формы (request_acquaintance) И повторно
    на «Отправить»/«Принять» (bot/pending_actions.py): за час/трое суток
    ожидания могли познакомиться иначе или прийти встречное предложение.
    ``ignore_action_id`` — сама проверяемая форма, она не конфликт себе.

    Падеж держим на служебном слове («адресату (…)»), имя — именительным
    лейблом в скобках: имя в карточке строго в именительном (живая находка
    2026-09-26, «у Наташа Сорокина»)."""
    pair = {initiator, target}
    for row in await store.open_pending_actions("relationship", initiator):
        if row["id"] == ignore_action_id or {row["initiator"], row["addressee"]} != pair:
            continue
        if row["initiator"] != initiator:
            # Встречное: адресат уже сам предложил знакомство инициатору.
            return (
                f"адресат ({target_name}) уже сам предложил вам знакомство — "
                "ответьте кнопкой «Принять» в его форме"
            )
        if row["status"] == "draft":
            return (
                f"форма предложения знакомства адресату ({target_name}) уже открыта — "
                "ждёт кнопки «Отправить» или «Отмена»"
            )
        # Живая находка 2026-10-09: этот ответ про Александра модель
        # пересказала как «отправил Андрею» — id был взят не тот.
        return (
            f"предложение знакомства уже отправлено адресату ({target_name}), ждём "
            f"ответа. Если собеседник говорил не про {target_name} — id не тот: "
            "найди нужного find_person и не говори, что ему уже отправлено"
        )
    if await are_acquainted(store, initiator, target):
        return f"вы с адресатом ({target_name}) уже знакомы"
    return None


async def tool_request_acquaintance(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Открыть инициатору форму предложения знакомства. Модель НЕ
    отправляет предложение и НЕ принимает ответ: тул создаёт черновик
    (bot/pending_actions.py, срок 1 ч), а форму с кнопками «Отправить»/
    «Отмена» бот пришлёт отдельным сообщением ПОСЛЕ ответа Альфреда
    (bot/handlers/ai.py → PendingActions.flush_drafts). Дальше всё решают
    кнопки.

    Адресат — по id (Этап 54.5): его находит find_person с
    purpose="acquaintance" — среди всех гостей, но только по точному имени,
    нику или прозвищу, без выдачи списка (guests_list гостю не виден).

    Только живой /ai: в службе tasks (ctx.pending_actions там нет) формы
    некому показать — честный отказ."""
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
    # Имена для ЧУЖОГО текста — не Subscription.name напрямую, см.
    # _guest_facing_name (владельческое "me" технически, не для показа).
    proposer_name = _guest_facing_name(ctx, ctx.chat_id, proposer.name)
    target_name = _guest_facing_name(ctx, target_chat_id, target.name)

    conflict = await acquaintance_conflict(ctx.store, ctx.chat_id, target_chat_id, target_name)
    if conflict is not None:
        return conflict
    await ctx.pending_actions.create_relationship_draft(
        ctx.chat_id, target_chat_id, RELATION_ACQUAINTANCE, proposer_name, target_name
    )
    # Живая находка 2026-09-26: инициатор — это ВСЕГДА сам собеседник в
    # этом чате, в тексте, адресованном ЕМУ, не называть его по имени в
    # третьем лице (Gemma озвучивала такое слово в слово).
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
    """Только подтверждённые знакомства — про неотвеченные/отклонённые
    предложения молчим намеренно (IMPLEMENTATION_PLAN.md §42.6.3).
    ``ctx.store`` нет в службе tasks — там честный отказ, не молчание."""
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
    """Подтверждённые знакомые собеседника: (chat_id, имя). id — главное:
    по нему tell находит человека точно, как бы модель ни переиначила имя
    (живой баг 2026-09-28: «передай маме» ушло в «Наталья Вадимовна», хотя
    знакома была Милана)."""
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
