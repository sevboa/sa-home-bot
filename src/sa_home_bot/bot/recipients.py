"""Кому Альфред может передать личное сообщение (тул ``tell``).

Задача одна: превратить то, как человека назвали в разговоре («передай
Андрею», «скажи @andrey»), в конкретный `chat_id` личного чата. Имена
человека знает его карточка (Этап 58, people/book.py::PeopleBook): имя,
прозвища, профиль Telegram, @ник, имя при приглашении. Без карточек (служба
tasks) — только имена подписок.

Два жёстких ограничения, без которых тул стал бы способом писать незнакомым
людям от чужого имени:

1. **Только подписной чат.** Нет подписки — нет и получателя, даже если имя
   совпало с карточкой. Право говорить с человеком даёт то же приглашение,
   что и всё остальное (AUTHORIZATION.md §10).
2. **Только личка.** У Telegram `chat_id` личного чата положителен и равен
   `user_id`, у групп — отрицателен. «Передать лично» в группу — это не
   лично, поэтому групповые чаты в кандидаты не попадают вовсе.

Неоднозначность не разрешаем догадками: если под «Андрею» подходит двое,
возвращаем оба варианта, а спрашивает уже сам Альфред.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

if TYPE_CHECKING:
    from sa_home_bot.people.book import PeopleBook

# Откуда узнали про человека — нужно только для пояснений модели.
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


def is_owner_role_reference(query: str) -> bool:
    """Назван не человек, а роль владельца («хозяину», «админу»)."""
    return _is_owner_role_reference(_norm(query))


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
    words = [w.lstrip("@") for w in candidate_norm.replace("(", " ").replace(")", " ").split()]
    if any(word.startswith(query) for word in words):
        return True
    # Несколько слов ("Алексей Севбо" при "Алексей Александрович Севбо") —
    # живой баг 2026-09-27: весь запрос целиком не начало ни одного слова.
    # Каждое слово запроса обязано быть началом СВОЕГО слова кандидата,
    # порядок не важен (фамилия может стоять первой).
    query_words = query.replace("(", " ").replace(")", " ").split()
    if len(query_words) < 2:
        return False
    free = list(words)
    for qw in query_words:
        hit = next((w for w in free if w.startswith(qw.lstrip("@"))), None)
        if hit is None:
            return False
        free.remove(hit)
    return True


def matches(query: str, candidate: str | None) -> bool:
    """Публичная обёртка _matches для поиска по описанию (bot/tools.py::
    tool_find_person) — та же строгость: целиком, по слову, по началу слова."""
    return _matches(_norm(query), candidate or "")


def query_chat_id(query: str) -> int | None:
    return _query_chat_id(query)


def find_by_chat_id(
    chat_id: int, book: SubscriptionBook, people: PeopleBook | None = None
) -> list[Recipient]:
    """Получатель по id — так адресуют tell/notify_guest/request_acquaintance
    с Этапа 54.5: id находит find_person, имя больше не угадывается."""
    return _find_by_chat_id(chat_id, book, people)


# "@ник" внутри запроса. Модель любит передавать адресата целым лейблом —
# "Наталья (@nava40a)", — где имя она уже успела переиначить (живой баг
# 2026-09-27: гостья вошла как "Наташа Сорокина (@nava40a)"). Юзернейм же
# уникален и не склоняется — если он назван, ищем только по нему.
_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{3,})")


def _query_handle(query: str) -> str | None:
    stripped = query.strip()
    match = _HANDLE_RE.search(stripped)
    if match is None:
        # Голый "@ник" без имени — обычный путь _matches и так справится.
        return None
    if stripped.lstrip("@") == match.group(1):
        return None
    return match.group(1).casefold()


# Telegram user_id внутри запроса ("Наташа (id 1243270013)"). С явным "id"
# — любой длины (так его выдают my_acquaintances и подсказки tell); голое
# число короче пяти цифр — это уже не id, а, скорее, номер/год в имени.
_EXPLICIT_ID_RE = re.compile(r"(?<!\w)id\s*:?\s*(\d+)(?!\w)", re.IGNORECASE)
_CHAT_ID_RE = re.compile(r"(?<![\w@])(\d{5,})(?!\w)")


def _query_chat_id(query: str) -> int | None:
    match = _EXPLICIT_ID_RE.search(query) or _CHAT_ID_RE.search(query)
    return int(match.group(1)) if match else None


def _has_handle(handle: str, candidate: str | None) -> bool:
    """Точное совпадение юзернейма — как поля, так и слова "(@ник)" в лейбле."""
    words = _norm(candidate or "").replace("(", " ").replace(")", " ").split()
    return any(word.lstrip("@") == handle for word in words)


def _is_private(chat_id: int) -> bool:
    return chat_id > 0


def _display(sub: Subscription, people: PeopleBook | None) -> str:
    shown = people.display(sub.chat_id) if people is not None else None
    return shown or sub.invited_user or sub.name


def _labels(sub: Subscription, people: PeopleBook | None) -> list[str]:
    if people is not None:
        return [label for _kind, label in people.labels(sub.chat_id)]
    return [label for label in (sub.name, sub.invited_user) if label]


def find_recipients(
    query: str,
    book: SubscriptionBook,
    people: PeopleBook | None = None,
) -> list[Recipient]:
    """Кандидаты под то, как человека назвали. Пусто — писать некому."""
    # Точные ключи сильнее имени: id и "@ник" уникальны, не склоняются и
    # не переиначиваются моделью. Названы — ищем только по ним.
    chat_id = _query_chat_id(query)
    if chat_id is not None:
        return _find_by_chat_id(chat_id, book, people)
    handle = _query_handle(query)
    if handle is not None:
        by_handle = [
            Recipient(sub.chat_id, _display(sub, people), SOURCE_SUBSCRIPTION)
            for sub in book.all()
            if _is_private(sub.chat_id)
            and any(_has_handle(handle, label) for label in _labels(sub, people))
        ]
        if by_handle:
            return by_handle
        # Юзернейм не наш (сменил ник?) — пробуем имя без скобки с ником.
        query = _HANDLE_RE.sub(" ", query).replace("(", " ").replace(")", " ")
        query = " ".join(query.split())
    wanted = _norm(query)
    if not wanted:
        return []

    found: dict[int, Recipient] = {}

    def remember(sub: Subscription, source: str) -> None:
        if _is_private(sub.chat_id):
            found.setdefault(sub.chat_id, Recipient(sub.chat_id, _display(sub, people), source))

    # Роль — первой: "передай владельцу" обязано дать SOURCE_OWNER_ROLE, даже
    # если владельца зовут тем же словом, — tell пускает к владельцу без
    # знакомства только по роли.
    if _is_owner_role_reference(wanted):
        for sub in book.all():
            if sub.is_owner:
                remember(sub, SOURCE_OWNER_ROLE)

    for sub in book.all():
        if any(_matches(wanted, label) for label in _labels(sub, people)):
            remember(sub, SOURCE_SUBSCRIPTION)

    return list(found.values())


def _find_by_chat_id(
    chat_id: int, book: SubscriptionBook, people: PeopleBook | None
) -> list[Recipient]:
    if not _is_private(chat_id):
        return []
    sub = book.for_chat(chat_id)
    if sub is None:
        return []
    return [Recipient(chat_id, _display(sub, people), SOURCE_SUBSCRIPTION)]
