"""Кому Альфред может передать личное сообщение (тул ``tell``).

Задача одна: превратить то, как человека назвали в разговоре («передай
Андрею», «скажи @andrey»), в конкретный `chat_id` личного чата. Источники
знаний два, и оба уже есть в системе:

- ``[[people]]`` из конфига — там есть `telegram_username`/`telegram_id` и
  полное имя, то есть «Андрей Иванов» находится по слову «Андрей»;
- подписки, включая гостевые — там имя, под которым человек вошёл
  (`invited_user`, «Андрей (@andrey)»).

Два жёстких ограничения, без которых тул стал бы способом писать незнакомым
людям от чужого имени:

1. **Только подписной чат.** Нет подписки — нет и получателя, даже если имя
   совпало с записью в ``[[people]]``. Право говорить с человеком даёт то же
   приглашение, что и всё остальное (AUTHORIZATION.md §10).
2. **Только личка.** У Telegram `chat_id` личного чата положителен и равен
   `user_id`, у групп — отрицателен. «Передать лично» в группу — это не
   лично, поэтому групповые чаты в кандидаты не попадают вовсе.

Неоднозначность не разрешаем догадками: если под «Андрею» подходит двое,
возвращаем оба варианта, а спрашивает уже сам Альфред.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sa_home_bot.config import PersonConfig
from sa_home_bot.subscriptions.book import SubscriptionBook

# Откуда узнали про человека — нужно только для пояснений модели.
SOURCE_PEOPLE = "people"
SOURCE_SUBSCRIPTION = "subscription"
# Гость назвал не имя, а роль владельца ("передай хозяину") — совсем не то
# же самое, что найти владельца по имени: узнав source, tool_tell метит
# доставленное сообщение, чтобы владелец видел разницу.
SOURCE_OWNER_ROLE = "owner_role"

# Начала слов, которыми гость называет владельца, не зная его личного имени.
# Стебель, а не полный список форм — "админ" покрывает и "админу", и
# "администратор"/"администратору" одним элементом.
_OWNER_ROLE_STEMS = (
    "хозя",  # хозяин/хозяину/хозяина/хозяином/хозяине
    "владел",  # владелец/владельцу/владельца/владельцем
    "админ",  # админ/админу и администратор/администратору
    "граф",  # граф/графу/графа/графом
    "собственник",  # собственник/собственнику/собственника/собственником
    "owner",
    "admin",
)


def _is_owner_role_reference(query: str) -> bool:
    return any(query.startswith(stem) for stem in _OWNER_ROLE_STEMS)


@dataclass(frozen=True)
class Recipient:
    chat_id: int
    display: str
    source: str


def _norm(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def _matches(query: str, candidate: str) -> bool:
    """Совпадает ли имя. Целиком, по слову или по началу слова.

    «Андрей» находит «Андрей Иванов» и «Андрюха» не находит — намеренно: в
    доставке личных сообщений лучше не угадать, чем угадать неверно и написать
    не тому человеку.
    """
    candidate_norm = _norm(candidate)
    if not query or not candidate_norm:
        return False
    if query == candidate_norm:
        return True
    # invited_user обычно выглядит как "Имя Фамилия (@username)" (bot/invites.py) —
    # без разбора скобки/"@" слово "(@username)" не начинается с "username",
    # и голый юзернейм-запрос никогда не находит человека, только имя (живой
    # баг 2026-08-01: "nava40a" не находил Наташу, "наташа" — находил).
    words = candidate_norm.replace("(", " ").replace(")", " ").split()
    return any(word.lstrip("@").startswith(query) for word in words)


def _is_private(chat_id: int) -> bool:
    return chat_id > 0


def find_recipients(
    query: str,
    book: SubscriptionBook,
    people: Sequence[PersonConfig] = (),
) -> list[Recipient]:
    """Кандидаты под то, как человека назвали. Пусто — писать некому."""
    wanted = _norm(query)
    if not wanted:
        return []

    found: dict[int, Recipient] = {}

    def remember(chat_id: int, display: str, source: str) -> None:
        # Первый источник выигрывает: people разбирается раньше, и его
        # full_name — лучшее из имён, что у нас есть.
        if _is_private(chat_id) and book.for_chat(chat_id) is not None:
            found.setdefault(chat_id, Recipient(chat_id, display, source))

    for person in people:
        if _matches(wanted, person.telegram_username) or _matches(wanted, person.full_name):
            if person.telegram_id:
                remember(person.telegram_id, person.full_name, SOURCE_PEOPLE)

    for sub in book.all():
        if _matches(wanted, sub.name) or _matches(wanted, sub.invited_user):
            remember(sub.chat_id, sub.invited_user or sub.name, SOURCE_SUBSCRIPTION)

    if _is_owner_role_reference(wanted):
        for sub in book.all():
            if sub.is_owner:
                remember(sub.chat_id, sub.invited_user or sub.name, SOURCE_OWNER_ROLE)

    return list(found.values())
