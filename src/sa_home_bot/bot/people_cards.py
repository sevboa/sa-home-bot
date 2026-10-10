"""Карточки людей в справке перед ходом: текст для модели (Этап 54.3,
с Этапа 58 карточки — people/book.py::PeopleBook из БД alfred).

Имён источников в карточке нет — только by_id; имя подставляет вызывающий
(``name_of``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sa_home_bot.people import claims as people_claims
from sa_home_bot.people.claims import Card

_GENDER_WORD = {
    people_claims.GENDER_MALE: "мужчина",
    people_claims.GENDER_FEMALE: "женщина",
}
_TITLE = {people_claims.GENDER_MALE: "сэр", people_claims.GENDER_FEMALE: "мадам"}

NameOf = Callable[[int], str | None]


def card_gender(card: Card | None) -> str | None:
    claim = (card or {}).get("gender")
    return claim.get("value") if isinstance(claim, dict) else None


def _is_self(claim: dict[str, Any]) -> bool:
    return claim.get("strength") == people_claims.STRENGTH_SELF


def _source(claim: dict[str, Any], name_of: NameOf) -> str:
    return name_of(int(claim.get("by_id") or 0)) or f"гость с id {claim.get('by_id')}"


def _by_gender(gender: str | None, male: str, female: str, unknown: str) -> str:
    if gender == people_claims.GENDER_MALE:
        return male
    if gender == people_claims.GENDER_FEMALE:
        return female
    return unknown


def speaker_card_note(card: Card | None, name_of: NameOf) -> str | None:
    """Строки о собеседнике: пол с источником и обращение, имя, прозвища."""
    if not card:
        return None
    parts: list[str] = []
    claim = card.get("gender")
    gender = card_gender(card)
    if isinstance(claim, dict) and gender in _GENDER_WORD:
        word, title = _GENDER_WORD[gender], _TITLE[gender]
        if _is_self(claim):
            said = _by_gender(gender, "сказал это о себе сам", "сказала это о себе сама", "")
            parts.append(
                f"Собеседник — {word}: {said}. Обращайся «{title}» и склоняй слова о "
                "собеседнике в этом роде. Это точно — по имени не угадывай. Если "
                "спросят, откуда знаешь, — с собственных слов собеседника."
            )
        else:
            source = _source(claim, name_of)
            parts.append(
                f"Собеседник — {word}: так говорит {source}, сам собеседник этого "
                f"не подтверждал. Обращайся «{title}», пока он(а) не скажет иначе. "
                f"Если спросят, откуда знаешь, — назови источник: {source}."
            )
    name = card.get("name")
    if isinstance(name, dict) and name.get("value"):
        if _is_self(name):
            whose = _by_gender(
                gender, "сам так представился", "сама так представилась", "так представились сами"
            )
        else:
            whose = f"так говорит {_source(name, name_of)}"
        parts.append(f"Собеседника зовут {name['value']} ({whose}).")
    aliases = [a["value"] for a in card.get("aliases") or [] if a.get("value")]
    if aliases:
        pronoun = _by_gender(gender, "Его", "Её", "Собеседника")
        parts.append(f"{pronoun} также зовут: " + ", ".join(aliases) + ".")
    return " ".join(parts) or None


def roster_line(person_id: int, display: str, card: Card | None, name_of: NameOf) -> str:
    """«Милана — id 202 — женщина (сама сказала)» для списка знакомых и
    участников: по этой строке модель и адресует, и склоняет."""
    bits = [display, f"id {person_id}"]
    gender = (card or {}).get("gender")
    if isinstance(gender, dict) and gender.get("value") in _GENDER_WORD:
        word = _GENDER_WORD[gender["value"]]
        if _is_self(gender):
            bits.append(f"{word} (с собственных слов)")
        else:
            bits.append(f"{word} (так говорит {_source(gender, name_of)})")
    aliases = [a["value"] for a in (card or {}).get("aliases") or [] if a.get("value")]
    if aliases:
        bits.append("зовут также " + ", ".join(aliases))
    return " — ".join(bits)
