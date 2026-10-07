"""Карточки людей (Этап 54): проверка утверждения и сборка карточки из
утверждений — чистая логика без Neo4j, чтобы её проверяли юниты.

Утверждение — «by_id сказал, что у subject_id поле field = value». Хранит их
graph_memory/service.py узлами (:PersonClaim) в разделе PEOPLE_GROUP_ID.

Правило веса (решение владельца 2026-10-08): сказанное человеком о себе —
сильное, из сильных действует последнее; сказанное знакомым — слабое и
работает, только пока сам человек не сказал иначе. У владельца особого веса
нет. Источник (by_id) сохраняется, чтобы Альфред мог ответить «откуда
знаешь» — имя источника подставляет бот, в графе только id.
"""

from __future__ import annotations

from typing import Any

from sa_home_bot.graph_memory.protocol import (
    CLAIM_STRENGTH_ACQUAINTANCE,
    CLAIM_STRENGTH_SELF,
    GENDER_FEMALE,
    GENDER_MALE,
    PERSON_FIELD_ALIAS,
    PERSON_FIELD_GENDER,
    PERSON_FIELD_NAME,
)

PERSON_FIELDS = (PERSON_FIELD_GENDER, PERSON_FIELD_NAME, PERSON_FIELD_ALIAS)
MAX_VALUE_CHARS = 64
# Прозвищ на карточке — не больше этого, свежие вперёд: справка перед
# каждым ходом не должна разрастаться от болтливого знакомого.
MAX_ALIASES = 8

_GENDER_WORDS = {
    GENDER_MALE: GENDER_MALE,
    "male": GENDER_MALE,
    "м": GENDER_MALE,
    "муж": GENDER_MALE,
    "мужской": GENDER_MALE,
    "мужчина": GENDER_MALE,
    GENDER_FEMALE: GENDER_FEMALE,
    "female": GENDER_FEMALE,
    "ж": GENDER_FEMALE,
    "жен": GENDER_FEMALE,
    "женский": GENDER_FEMALE,
    "женщина": GENDER_FEMALE,
}


class ClaimError(ValueError):
    """Утверждение не прошло проверку — текст годится для ответа модели."""


def strength_for(subject_id: int, by_id: int) -> str:
    return CLAIM_STRENGTH_SELF if subject_id == by_id else CLAIM_STRENGTH_ACQUAINTANCE


def normalize_claim(field: str, value: Any) -> tuple[str, str]:
    """(field, value) в каноническом виде или ClaimError."""
    field = str(field or "").strip().lower()
    if field not in PERSON_FIELDS:
        raise ClaimError(f"неизвестное поле {field!r}, есть: {', '.join(PERSON_FIELDS)}")
    text = " ".join(str(value or "").split())
    if not text:
        raise ClaimError(f"пустое значение для {field}")
    if field == PERSON_FIELD_GENDER:
        gender = _GENDER_WORDS.get(text.lower())
        if gender is None:
            raise ClaimError(f"пол — {GENDER_MALE} или {GENDER_FEMALE}, а не {text!r}")
        return field, gender
    if len(text) > MAX_VALUE_CHARS:
        raise ClaimError(f"слишком длинно ({len(text)} знаков, максимум {MAX_VALUE_CHARS})")
    return field, text


def _pick(claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Последнее сильное, иначе последнее слабое."""
    for strength in (CLAIM_STRENGTH_SELF, CLAIM_STRENGTH_ACQUAINTANCE):
        same = [c for c in claims if c["strength"] == strength]
        if same:
            return max(same, key=lambda c: c["at"])
    return None


def _public(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": claim["value"],
        "by_id": claim["by_id"],
        "strength": claim["strength"],
        "at": claim["at"],
    }


def build_cards(claims: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Утверждения (subject_id, field, value, by_id, strength, at) → карточки
    по subject_id: {"gender": {...}|None, "name": {...}|None, "aliases": [...]}.

    Прозвища не вытесняют друг друга (у человека их может быть несколько),
    одно значение — одна запись: сильная версия, иначе свежайшая."""
    by_subject: dict[int, list[dict[str, Any]]] = {}
    for claim in claims:
        by_subject.setdefault(int(claim["subject_id"]), []).append(claim)
    cards: dict[int, dict[str, Any]] = {}
    for subject_id, own in by_subject.items():
        card: dict[str, Any] = {}
        for field in (PERSON_FIELD_GENDER, PERSON_FIELD_NAME):
            picked = _pick([c for c in own if c["field"] == field])
            card[field] = _public(picked) if picked else None
        aliases: dict[str, dict[str, Any]] = {}
        for claim in own:
            if claim["field"] != PERSON_FIELD_ALIAS:
                continue
            key = claim["value"].casefold()
            aliases[key] = _pick([c for c in (aliases.get(key), claim) if c])  # type: ignore[assignment]
        card["aliases"] = [
            _public(c) for c in sorted(aliases.values(), key=lambda c: c["at"], reverse=True)
        ][:MAX_ALIASES]
        cards[subject_id] = card
    return cards
