from __future__ import annotations

from typing import Any

from sa_home_bot.graph_memory.protocol import (
    GENDER_FEMALE,
    GENDER_MALE,
    PERSON_FIELD_ALIAS,
    PERSON_FIELD_GENDER,
    PERSON_FIELD_NAME,
)
from sa_home_bot.people.claims import (
    GENDER_WORDS,
    MAX_VALUE_CHARS,
    ClaimError,
    collect_aliases,
    group_by_subject,
    picked_claim,
    strength_for,
)

PERSON_FIELDS = (PERSON_FIELD_GENDER, PERSON_FIELD_NAME, PERSON_FIELD_ALIAS)

__all__ = ["ClaimError", "PERSON_FIELDS", "build_cards", "normalize_claim", "strength_for"]


def normalize_claim(field: str, value: Any) -> tuple[str, str]:
    field = str(field or "").strip().lower()
    if field not in PERSON_FIELDS:
        raise ClaimError(f"неизвестное поле {field!r}, есть: {', '.join(PERSON_FIELDS)}")
    text = " ".join(str(value or "").split())
    if not text:
        raise ClaimError(f"пустое значение для {field}")
    if field == PERSON_FIELD_GENDER:
        gender = GENDER_WORDS.get(text.lower())
        if gender is None:
            raise ClaimError(f"пол — {GENDER_MALE} или {GENDER_FEMALE}, а не {text!r}")
        return field, gender
    if len(text) > MAX_VALUE_CHARS:
        raise ClaimError(f"слишком длинно ({len(text)} знаков, максимум {MAX_VALUE_CHARS})")
    return field, text


def build_cards(claims: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    cards: dict[int, dict[str, Any]] = {}
    for subject_id, own in group_by_subject(claims).items():
        card: dict[str, Any] = {
            field: picked_claim(own, field) for field in (PERSON_FIELD_GENDER, PERSON_FIELD_NAME)
        }
        card["aliases"] = collect_aliases(own)
        cards[subject_id] = card
    return cards
