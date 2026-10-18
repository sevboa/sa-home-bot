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

SINGLE_FIELDS = (FIELD_GENDER, FIELD_NAME, FIELD_BIRTH_DATE, FIELD_CITY, FIELD_TIMEZONE)
FIELDS = (*SINGLE_FIELDS, FIELD_ALIAS)
MAX_VALUE_CHARS = 64
MAX_ALIASES = 8

GENDER_WORDS = {
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
    pass


def strength_for(subject_id: int, by_id: int) -> str:
    return STRENGTH_SELF if subject_id == by_id else STRENGTH_ACQUAINTANCE


def normalize_claim(field: str, value: Any) -> tuple[str, str]:
    field = str(field or "").strip().lower()
    if field not in FIELDS:
        raise ClaimError(f"неизвестное поле {field!r}, есть: {', '.join(FIELDS)}")
    text = " ".join(str(value or "").split())
    if not text:
        raise ClaimError(f"пустое значение для {field}")
    if field == FIELD_GENDER:
        gender = GENDER_WORDS.get(text.lower())
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


def pick_claim(claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    for strength in (STRENGTH_SELF, STRENGTH_ACQUAINTANCE):
        same = [c for c in claims if c["strength"] == strength]
        if same:
            return max(same, key=lambda c: c["at"])
    return None


def public_claim(claim: dict[str, Any]) -> dict[str, Any]:
    return {
        "value": claim["value"],
        "by_id": claim["by_id"],
        "strength": claim["strength"],
        "at": claim["at"],
    }


def group_by_subject(claims: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    by_subject: dict[int, list[dict[str, Any]]] = {}
    for claim in claims:
        by_subject.setdefault(int(claim["subject_id"]), []).append(claim)
    return by_subject


def picked_claim(own: list[dict[str, Any]], field: str) -> dict[str, Any] | None:
    picked = pick_claim([c for c in own if c["field"] == field])
    return public_claim(picked) if picked else None


def collect_aliases(own: list[dict[str, Any]]) -> list[dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    for claim in own:
        if claim["field"] != FIELD_ALIAS:
            continue
        key = claim["value"].casefold()
        aliases[key] = pick_claim([c for c in (aliases.get(key), claim) if c])  # type: ignore[assignment]
    return [
        public_claim(c) for c in sorted(aliases.values(), key=lambda c: c["at"], reverse=True)
    ][:MAX_ALIASES]


def build_cards(claims: list[dict[str, Any]]) -> dict[int, Card]:
    cards: dict[int, Card] = {}
    for subject_id, own in group_by_subject(claims).items():
        card: Card = {field: picked_claim(own, field) for field in SINGLE_FIELDS}
        card["aliases"] = collect_aliases(own)
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
