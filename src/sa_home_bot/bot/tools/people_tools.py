from __future__ import annotations

from typing import Any

from sa_home_bot.bot import (
    recipients,
)
from sa_home_bot.bot.tools.acquaintance import acquaintance_roster, are_acquainted
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec, people_of
from sa_home_bot.bot.tools.translit import CYRILLIC_TO_LATIN
from sa_home_bot.people import claims as people_claims
from sa_home_bot.people.book import PeopleBook
from sa_home_bot.utils.clock import utc_now


def _note_person_stranger(ctx: ToolContext, subject_id: int, name: str) -> str:
    """Отказ note_person о незнакомом — с тем, чей это id.

    Живая находка 2026-10-09: «Я знаком с Андреем, это мой брат» — модель
    без find_person взяла id Александра — единственный id в семейных фактах
    памяти (у Андрея там id нет), а безымянный отказ ещё и подсказал открыть
    знакомство с ним же."""
    if ctx.book is None:
        who = "этот человек"
    else:
        if ctx.book.for_chat(subject_id) is None:
            return (
                f"не записал: id {subject_id} — ни один гость. id не придумывай: "
                f'найди человека find_person(description="{name or "как его назвали"}")'
            )
        own = people_of(ctx).labels(subject_id)
        who = people_of(ctx).display(subject_id) or f"id {subject_id}"
        if name.strip() and not _name_fits(name, own):
            return (
                f"не записал: id {subject_id} — это {who}, а не «{name}». id из памяти и "
                f'прошлых разговоров не бери: найди нужного find_person(description="{name}", '
                'purpose="acquaintance")'
            )
    return (
        f"не записал: о других запоминаю только со слов их знакомых, а собеседник "
        f"и {who} не знакомы через тебя; познакомить — "
        f"request_acquaintance(recipient_id={subject_id})"
    )


async def tool_note_person(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Записать о человеке пол, имя, прозвища, день рождения, город или
    часовой пояс в его карточку (Этап 58, таблица person_claims) — её видят
    все чаты, а не только этот.

    Вес решает бот, не модель: говорящий о себе — сильное утверждение;
    о знакомом — слабое; о постороннем — отказ."""
    if ctx.store is None:
        return "недоступно: карточки людей сейчас не записать"
    by_id = ctx.user_id
    if by_id is None:
        return "недоступно: не знаю, кто сейчас говорит"
    raw_subject = args.get("person_id")
    try:
        subject_id = by_id if raw_subject in (None, "") else int(raw_subject)
    except (TypeError, ValueError):
        return f"ошибка: person_id — это Telegram id числом, а не {raw_subject!r}"
    if subject_id != by_id and not await are_acquainted(ctx.store, by_id, subject_id):
        return _note_person_stranger(ctx, subject_id, str(args.get("name") or ""))
    claims: list[tuple[str, Any]] = []
    for key in people_claims.SINGLE_FIELDS:
        if args.get(key) not in (None, ""):
            claims.append((key, args[key]))
    aliases = args.get("alias")
    for alias in [aliases] if isinstance(aliases, str) else list(aliases or []):
        if str(alias).strip():
            claims.append((people_claims.FIELD_ALIAS, alias))
    if not claims:
        return (
            "ошибка: нечего записывать — укажи gender, name, alias, birth_date, city "
            "или timezone"
        )
    try:
        normalized = [people_claims.normalize_claim(key, value) for key, value in claims]
    except people_claims.ClaimError as exc:
        return f"ошибка: {exc}"
    at = utc_now().isoformat()
    strength = people_claims.strength_for(subject_id, by_id)
    await ctx.store.add_person_claims(
        [
            {
                "subject_id": subject_id,
                "field": key,
                "value": value,
                "by_id": by_id,
                "strength": strength,
                "at": at,
            }
            for key, value in normalized
        ]
    )
    ctx.people = await PeopleBook.load(ctx.store, ctx.book)
    whose = "со слов самого человека" if subject_id == by_id else "со слов знакомого (слабее)"
    saved = ", ".join(f"{key}={value}" for key, value in normalized)
    return f"записал в карточку id {subject_id}, {whose}: {saved}"


_DECL_NOTE_PERSON: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "note_person",
        "description": (
            "Запомнить навсегда и для всех разговоров, кто человек: пол, имя, "
            "прозвище, день рождения, город, часовой пояс. Зови, когда "
            "собеседник прямо сказал это о себе («я парень», «зови меня "
            "Лилиан», «я живу в Казани») или о знакомом ему госте — и сразу, "
            "когда тебя поправили в обращении. По имени или догадке не зови: "
            "только сказанное словами. О себе — person_id не указывай. "
            "Такие сведения о людях — только сюда, не в memory."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "person_id": {
                    "type": "integer",
                    "description": "Telegram id знакомого, если речь о нём, а не о собеседнике",
                },
                "gender": {"type": "string", "enum": ["m", "f"]},
                "name": {"type": "string", "description": "как человека зовут"},
                "alias": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "прозвища и уменьшительные имена",
                },
                "birth_date": {"type": "string", "description": "день рождения, ГГГГ-ММ-ДД"},
                "city": {"type": "string", "description": "город, где живёт"},
                "timezone": {
                    "type": "string",
                    "description": "часовой пояс IANA, например Europe/Moscow",
                },
            },
            "required": [],
        },
    },
}


PURPOSE_MESSAGE = "message"
PURPOSE_ACQUAINTANCE = "acquaintance"
_GENDER_WORDS = {"m": "мужчина", "f": "женщина"}


def _stem(word: str) -> str:
    word = word.casefold().lstrip("@")
    return word[:-1] if len(word) > 3 else word


def _same_stems(query: str, candidate: str) -> bool:
    """Падеж: «Миле» и «Мила», «Андрею» и «Андрей» — у каждого слова
    запроса есть слово кандидата с той же основой (без последней буквы).
    Строже, чем начало слова: «Миле» не находит «Милану»."""
    words = [_stem(w) for w in candidate.replace("(", " ").replace(")", " ").split()]
    query_words = [_stem(w) for w in query.split()]
    return bool(query_words) and all(w in words for w in query_words)


def _stem_query(query: str) -> str:
    return " ".join(_stem(w) for w in query.split())


# Слова описания, которые не имя: «Андрей который кейн», «это мой отец».
_DESCRIPTION_FILLER = frozenset(
    "который которая которое которые это этот эта мой моя моё мое мои твой "
    "его её ее их есть такой такая как тот та кто зовут зовёт по нику ник "
    "имени имя с со и или он она".split()
)


def _nick_spellings(word: str) -> set[str]:
    """Кириллицей написанный ник латиницей: «кейн» → kein/keyn/kejn."""
    base = word.translate(CYRILLIC_TO_LATIN)
    if base == word:
        return set()
    spellings = {base}
    for cyr, lat in (("й", ("i", "j")), ("х", ("h",)), ("е", ("ye",))):
        if cyr in word:
            for alt in lat:
                spellings.add(word.replace(cyr, alt).translate(CYRILLIC_TO_LATIN))
    return spellings


def _match_by_words(
    query: str, labels: dict[int, list[tuple[str, str]]]
) -> dict[int, str]:
    """Последний заход find_person: описание целиком не совпало ни с кем —
    («Андрей который кейн», живая находка 2026-10-09) — ищем по отдельным
    словам без служебных, и кириллицу ещё транслитом (ник «kein»). Берём
    тех, у кого совпало больше всего слов; несколько — выберет собеседник."""
    words = [
        w
        for w in (w.strip(".,!?«»\"'()").casefold() for w in query.split())
        if len(w) >= 3 and w not in _DESCRIPTION_FILLER
    ]
    scores: dict[int, list[str]] = {}
    for word in words:
        variants = {word, _stem(word)} | _nick_spellings(word)
        for chat_id, own in labels.items():
            if any(
                label and recipients.matches(v, label) for v in variants for _kind, label in own
            ):
                scores.setdefault(chat_id, []).append(word)
    if not scores:
        return {}
    best = max(len(found) for found in scores.values())
    return {
        chat_id: "по словам «" + "», «".join(found) + "»"
        for chat_id, found in scores.items()
        if len(found) == best
    }


def _name_fits(name: str, own: list[tuple[str, str]]) -> bool:
    """Подходит ли названное имя к меткам одного человека — те же заходы,
    что у find_person, кроме id и роли."""
    for _kind, label in own:
        if label and (
            recipients.matches(name, label)
            or _same_stems(name, label)
            or recipients.matches(_stem_query(name), label)
        ):
            return True
    return bool(_match_by_words(name, {0: own}))


async def tool_find_person(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Этап 54.4: найти id человека по описанию. Ищет только среди тех,
    к кому собеседнику вообще можно обратиться:

    - purpose="message" (tell, notify_guest): у гостя — подтверждённые
      знакомые и роль владельца; у владельца — все гости в личке;
    - purpose="acquaintance" (request_acquaintance): все гости, но если
      никто не подошёл, список не выдаём — гостю он не положен.

    Совпадения — по всем именам карточки человека (Этап 58: имена,
    прозвища, профиль Telegram, @ник, имя при приглашении); вторым заходом —
    без последней буквы (падеж).
    Несколько кандидатов — отдаём всех с отличиями, выбирает собеседник."""
    if ctx.chat_id is None or ctx.book is None:
        return "недоступно: сейчас не вижу, кому можно писать"
    query = " ".join(str(args.get("description") or "").split())
    if not query:
        return "ошибка: опиши человека (description) — как его назвал собеседник"
    purpose = str(args.get("purpose") or PURPOSE_MESSAGE).strip()
    if purpose not in (PURPOSE_MESSAGE, PURPOSE_ACQUAINTANCE):
        purpose = PURPOSE_MESSAGE
    is_owner = ctx.subscription is not None and ctx.subscription.is_owner

    private = {sub.chat_id: sub for sub in ctx.book.all() if sub.chat_id > 0}
    if purpose == PURPOSE_ACQUAINTANCE or is_owner:
        pool = set(private)
    else:
        pool = {chat_id for chat_id, _name in await acquaintance_roster(ctx)} & set(private)
    if not (is_owner and purpose == PURPOSE_MESSAGE):
        pool.discard(ctx.chat_id)

    people = people_of(ctx)
    labels = {chat_id: people.labels(chat_id) for chat_id in pool}

    def display(chat_id: int) -> str:
        return people.display(chat_id) or str(chat_id)

    gender_of = people.gender

    hits: dict[int, str] = {}
    explicit = recipients.query_chat_id(query)
    if explicit is not None and explicit in pool:
        hits[explicit] = "по id"
    if not hits and purpose == PURPOSE_MESSAGE and recipients.is_owner_role_reference(query):
        for chat_id, sub in private.items():
            if sub.is_owner and chat_id != ctx.chat_id:
                hits[chat_id] = "роль владельца — передавай с to_owner_role=true"
    # @ник в описании точнее любого имени рядом с ним: «Александр
    # Сергеевич Севбо @ksytal_as» целиком не совпадал ни с одной меткой
    # (живая находка 2026-10-09).
    for nick in (w for w in query.split() if w.startswith("@") and len(w) > 1):
        for chat_id, own in labels.items():
            if any(label and recipients.matches(nick, label) for _kind, label in own):
                hits.setdefault(chat_id, f"ник «{nick}»")
    # Три захода, от строгого к мягкому: как названо; та же основа
    # (падеж); начало слова по основе. Следующий — только если пусто.
    matchers = (
        lambda label: recipients.matches(query, label),
        lambda label: _same_stems(query, label),
        lambda label: recipients.matches(_stem_query(query), label),
    )
    for matcher in matchers:
        if hits:
            break
        for chat_id, own in labels.items():
            for kind, label in own:
                if label and matcher(label):
                    hits.setdefault(chat_id, f"{kind} «{label}»")
    if not hits:
        hits = _match_by_words(query, labels)

    if not hits:
        if purpose == PURPOSE_ACQUAINTANCE:
            return (
                f"под «{query}» никого из гостей не нашёл — переспроси имя или ник; "
                "познакомить можно только с тем, кто уже принял приглашение"
            )
        allowed = [
            f"{display(c)} — id {c}" + (f" — {_GENDER_WORDS[g]}" if (g := gender_of(c)) else "")
            for c in sorted(pool)
        ]
        tail = (
            " Кому можно писать: " + "; ".join(allowed) + "."
            if allowed
            else " Подтверждённых знакомых у собеседника нет."
        )
        if not is_owner:
            tail += " Владельцу можно писать по роли: «владельцу»/«хозяину»."
        return (
            f"под «{query}» никого не нашёл. Родственное слово («маме») — не имя: "
            "если по списку ясно, кто это, бери его id, иначе переспроси." + tail
        )

    lines = []
    for chat_id, reason in hits.items():
        gender = gender_of(chat_id)
        bits = [display(chat_id), f"id {chat_id}"]
        if gender:
            bits.append(_GENDER_WORDS[gender])
        bits.append(f"совпало: {reason}")
        lines.append(" — ".join(bits))
    if len(lines) == 1:
        if purpose == PURPOSE_ACQUAINTANCE:
            (chat_id,) = hits
            return f"Нашёл: {lines[0]}; познакомить — request_acquaintance(recipient_id={chat_id})"
        return "Нашёл: " + lines[0]
    return (
        "Подходят несколько — переспроси собеседника, кого он имел в виду, "
        "сам не выбирай:\n" + "\n".join(lines)
    )


_DECL_FIND_PERSON: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "find_person",
        "description": (
            "Найти Telegram id человека по описанию — для tell, notify_guest и "
            "request_acquaintance, которые принимают адресата только по id. "
            "Передай, как собеседник назвал человека: имя в любом падеже, "
            "прозвище, @ник, «хозяину». Ищет только среди тех, кому собеседнику "
            "можно писать (purpose=message), или среди всех гостей для "
            "знакомства (purpose=acquaintance). Несколько кандидатов — "
            "переспроси, никого — скажи как есть, не сочиняй id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "как собеседник назвал или описал человека",
                },
                "purpose": {
                    "type": "string",
                    "enum": [PURPOSE_MESSAGE, PURPOSE_ACQUAINTANCE],
                    "description": "message — чтобы написать; acquaintance — чтобы познакомить",
                },
            },
            "required": ["description"],
        },
    },
}


NOTE_PERSON = ToolSpec(
    name="note_person", handler=tool_note_person, declaration=_DECL_NOTE_PERSON
)

FIND_PERSON = ToolSpec(
    name="find_person", handler=tool_find_person, declaration=_DECL_FIND_PERSON
)
