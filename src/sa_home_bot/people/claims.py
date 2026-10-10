"""Утверждения о человеке и карточка из них (Этап 58) — чистая логика без
БД, чтобы её проверяли юниты.

Утверждение — «by_id сказал, что у subject_id поле field = value». Хранит их
таблица ``person_claims`` (db/schema.sql) на alfred.

Правило веса (решение владельца 2026-10-08, этап 54): сказанное человеком о
себе — сильное, из сильных действует последнее; сказанное знакомым — слабое
и работает, только пока сам человек не сказал иначе. У владельца особого
веса нет. Источник (by_id) сохраняется, чтобы Альфред мог ответить «откуда
знаешь».
"""

from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FIELD_GENDER = "gender"
FIELD_NAME = "name"
FIELD_ALIAS = "alias"
FIELD_BIRTH_DATE = "birth_date"
FIELD_CITY = "city"
FIELD_TIMEZONE = "timezone"
GENDER_MALE = "m"
GENDER_FEMALE = "f"
STRENGTH_SELF = "self"
STRENGTH_ACQUAINTANCE = "acquaintance"

# Поля с одним итоговым значением; прозвищ — несколько.
SINGLE_FIELDS = (FIELD_GENDER, FIELD_NAME, FIELD_BIRTH_DATE, FIELD_CITY, FIELD_TIMEZONE)
FIELDS = (*SINGLE_FIELDS, FIELD_ALIAS)
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

Card = dict[str, Any]


class ClaimError(ValueError):
    """Утверждение не прошло проверку — текст годится для ответа модели."""


def strength_for(subject_id: int, by_id: int) -> str:
    return STRENGTH_SELF if subject_id == by_id else STRENGTH_ACQUAINTANCE


def normalize_claim(field: str, value: Any) -> tuple[str, str]:
    """(field, value) в каноническом виде или ClaimError."""
    field = str(field or "").strip().lower()
    if field not in FIELDS:
        raise ClaimError(f"неизвестное поле {field!r}, есть: {', '.join(FIELDS)}")
    text = " ".join(str(value or "").split())
    if not text:
        raise ClaimError(f"пустое значение для {field}")
    if field == FIELD_GENDER:
        gender = _GENDER_WORDS.get(text.lower())
        if gender is None:
            raise ClaimError(f"пол — {GENDER_MALE} или {GENDER_FEMALE}, а не {text!r}")
        return field, gender
    if field == FIELD_BIRTH_DATE:
        try:
            return field, date.fromisoformat(text).isoformat()
        except ValueError:
            raise ClaimError(f"дата рождения — ГГГГ-ММ-ДД, а не {text!r}") from None
    if field == FIELD_TIMEZONE:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            raise ClaimError(
                f"часовой пояс — имя IANA вроде Europe/Moscow, а не {text!r}"
            ) from None
        return field, text
    if len(text) > MAX_VALUE_CHARS:
        raise ClaimError(f"слишком длинно ({len(text)} знаков, максимум {MAX_VALUE_CHARS})")
    return field, text


def _pick(claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Последнее сильное, иначе последнее слабое."""
    for strength in (STRENGTH_SELF, STRENGTH_ACQUAINTANCE):
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


def build_cards(claims: list[dict[str, Any]]) -> dict[int, Card]:
    """Утверждения → карточки по subject_id: на каждое поле из SINGLE_FIELDS
    итог ({value, by_id, strength, at} или None), ``aliases`` — прозвища,
    ``names`` — все когда-либо названные имена, свежие вперёд (по ним ищут:
    человек мог представиться иначе, а знают его по-прежнему).

    Прозвища не вытесняют друг друга, одно значение — одна запись: сильная
    версия, иначе свежайшая."""
    by_subject: dict[int, list[dict[str, Any]]] = {}
    for claim in claims:
        by_subject.setdefault(int(claim["subject_id"]), []).append(claim)
    cards: dict[int, Card] = {}
    for subject_id, own in by_subject.items():
        card: Card = {}
        for field in SINGLE_FIELDS:
            picked = _pick([c for c in own if c["field"] == field])
            card[field] = _public(picked) if picked else None
        aliases: dict[str, dict[str, Any]] = {}
        for claim in own:
            if claim["field"] != FIELD_ALIAS:
                continue
            key = claim["value"].casefold()
            aliases[key] = _pick([c for c in (aliases.get(key), claim) if c])  # type: ignore[assignment]
        card["aliases"] = [
            _public(c) for c in sorted(aliases.values(), key=lambda c: c["at"], reverse=True)
        ][:MAX_ALIASES]
        names: list[str] = []
        for claim in sorted(own, key=lambda c: c["at"], reverse=True):
            if claim["field"] == FIELD_NAME and claim["value"] not in names:
                names.append(claim["value"])
        card["names"] = names
        cards[subject_id] = card
    return cards


def value(card: Card | None, field: str) -> str | None:
    claim = (card or {}).get(field)
    return claim.get("value") if isinstance(claim, dict) else None
