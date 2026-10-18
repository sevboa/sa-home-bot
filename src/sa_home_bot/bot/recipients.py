from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sa_home_bot.subscriptions.book import SubscriptionBook
from sa_home_bot.subscriptions.models import Subscription

if TYPE_CHECKING:
    from sa_home_bot.people.book import PeopleBook

SOURCE_SUBSCRIPTION = "subscription"
SOURCE_OWNER_ROLE = "owner_role"

_OWNER_ROLE_STEMS = (
    "хозя",
    "владел",
    "админ",
    "граф",
    "собственник",
    "owner",
    "admin",
)


def is_owner_role_reference(query: str) -> bool:
    normalized = _normalize_label(query)
    return any(normalized.startswith(stem) for stem in _OWNER_ROLE_STEMS)


@dataclass(frozen=True)
class Recipient:
    chat_id: int
    display: str
    source: str


def _normalize_label(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def matches(query: str, candidate: str | None) -> bool:
    query = _normalize_label(query)
    candidate_norm = _normalize_label(candidate or "")
    if not query or not candidate_norm:
        return False
    if query == candidate_norm:
        return True
    words = [w.lstrip("@") for w in candidate_norm.replace("(", " ").replace(")", " ").split()]
    if any(word.startswith(query) for word in words):
        return True
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


_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{3,})")


def _query_handle(query: str) -> str | None:
    stripped = query.strip()
    match = _HANDLE_RE.search(stripped)
    if match is None:
        return None
    if stripped.lstrip("@") == match.group(1):
        return None
    return match.group(1).casefold()


_EXPLICIT_ID_RE = re.compile(r"(?<!\w)id\s*:?\s*(\d+)(?!\w)", re.IGNORECASE)
_CHAT_ID_RE = re.compile(r"(?<![\w@])(\d{5,})(?!\w)")


def query_chat_id(query: str) -> int | None:
    match = _EXPLICIT_ID_RE.search(query) or _CHAT_ID_RE.search(query)
    return int(match.group(1)) if match else None


def _label_has_handle(handle: str, candidate: str | None) -> bool:
    words = _normalize_label(candidate or "").replace("(", " ").replace(")", " ").split()
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
    chat_id = query_chat_id(query)
    if chat_id is not None:
        return find_by_chat_id(chat_id, book, people)
    handle = _query_handle(query)
    if handle is not None:
        by_handle = [
            Recipient(sub.chat_id, _display(sub, people), SOURCE_SUBSCRIPTION)
            for sub in book.all()
            if _is_private(sub.chat_id)
            and any(_label_has_handle(handle, label) for label in _labels(sub, people))
        ]
        if by_handle:
            return by_handle
        query = _HANDLE_RE.sub(" ", query).replace("(", " ").replace(")", " ")
        query = " ".join(query.split())
    wanted = _normalize_label(query)
    if not wanted:
        return []

    found: dict[int, Recipient] = {}

    def remember(sub: Subscription, source: str) -> None:
        if _is_private(sub.chat_id):
            found.setdefault(sub.chat_id, Recipient(sub.chat_id, _display(sub, people), source))

    if is_owner_role_reference(wanted):
        for sub in book.all():
            if sub.is_owner:
                remember(sub, SOURCE_OWNER_ROLE)

    for sub in book.all():
        if any(matches(wanted, label) for label in _labels(sub, people)):
            remember(sub, SOURCE_SUBSCRIPTION)

    return list(found.values())


def find_by_chat_id(
    chat_id: int, book: SubscriptionBook, people: PeopleBook | None = None
) -> list[Recipient]:
    if not _is_private(chat_id):
        return []
    sub = book.for_chat(chat_id)
    if sub is None:
        return []
    return [Recipient(chat_id, _display(sub, people), SOURCE_SUBSCRIPTION)]
