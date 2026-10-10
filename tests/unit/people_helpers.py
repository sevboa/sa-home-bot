"""Карточки людей для тестов (Этап 58): PeopleBook из коротких описаний."""

from __future__ import annotations

from typing import Any

from sa_home_bot.people.book import PeopleBook

OWNER = 188548043


def claim(
    subject_id: int,
    field: str,
    value: str,
    by_id: int | None = None,
    at: str = "2026-10-01T00:00:00+00:00",
) -> dict[str, Any]:
    by = subject_id if by_id is None else by_id
    return {
        "subject_id": subject_id,
        "field": field,
        "value": value,
        "by_id": by,
        "strength": "self" if by == subject_id else "acquaintance",
        "at": at,
    }


def people_book(
    book: Any | None = None,
    cards: dict[int, dict[str, Any]] | None = None,
    profiles: dict[int, tuple[str, str]] | None = None,
    by_id: int = OWNER,
) -> PeopleBook:
    """``cards`` — {id: {"name": …, "gender": …, "alias": [...], …}}, слово
    ``by_id`` (по умолчанию владельца; о самом владельце — его же, сильное).
    ``profiles`` — {id: (имя в Telegram, ник)}."""
    claims: list[dict[str, Any]] = []
    for subject_id, fields in (cards or {}).items():
        for field, value in fields.items():
            for one in value if isinstance(value, list) else [value]:
                claims.append(claim(subject_id, field, one, by_id))
    rows = [
        {"user_id": uid, "first_name": name, "last_name": "", "username": nick}
        for uid, (name, nick) in (profiles or {}).items()
    ]
    return PeopleBook(claims, rows, book)
