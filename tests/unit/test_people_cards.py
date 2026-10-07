"""Карточки людей (Этап 54): вес утверждений и сборка карточки."""

from __future__ import annotations

import pytest

from sa_home_bot.graph_memory.people import (
    MAX_ALIASES,
    ClaimError,
    build_cards,
    normalize_claim,
    strength_for,
)

LILIAN = 101
MILANA = 202
OWNER = 303


def claim(field, value, by_id, at, subject_id=LILIAN):
    return {
        "subject_id": subject_id,
        "field": field,
        "value": value,
        "by_id": by_id,
        "strength": strength_for(subject_id, by_id),
        "at": at,
    }


def test_self_claim_beats_a_newer_acquaintance_claim():
    cards = build_cards(
        [
            claim("gender", "m", LILIAN, "2026-10-01"),
            claim("gender", "f", MILANA, "2026-10-05"),
        ]
    )
    assert cards[LILIAN]["gender"]["value"] == "m"
    assert cards[LILIAN]["gender"]["strength"] == "self"


def test_latest_self_claim_wins():
    cards = build_cards(
        [
            claim("name", "Лилиан", LILIAN, "2026-10-01"),
            claim("name", "Лиля", LILIAN, "2026-10-03"),
        ]
    )
    assert cards[LILIAN]["name"]["value"] == "Лиля"


def test_owner_has_no_special_weight():
    cards = build_cards(
        [
            claim("gender", "f", OWNER, "2026-10-06"),
            claim("gender", "m", MILANA, "2026-10-07"),
        ]
    )
    assert cards[LILIAN]["gender"]["value"] == "m"
    assert cards[LILIAN]["gender"]["by_id"] == MILANA
    assert cards[LILIAN]["gender"]["strength"] == "acquaintance"


def test_aliases_accumulate_and_keep_the_strong_version():
    cards = build_cards(
        [
            claim("alias", "Лиля", MILANA, "2026-10-02"),
            claim("alias", "лиля", LILIAN, "2026-10-01"),
            claim("alias", "Ли", MILANA, "2026-10-03"),
        ]
    )
    aliases = cards[LILIAN]["aliases"]
    assert [a["value"] for a in aliases] == ["Ли", "лиля"]
    assert aliases[1]["strength"] == "self"


def test_aliases_are_capped():
    claims = [claim("alias", f"a{i}", MILANA, f"2026-10-{i + 1:02d}") for i in range(12)]
    assert len(build_cards(claims)[LILIAN]["aliases"]) == MAX_ALIASES


def test_missing_fields_are_none():
    cards = build_cards([claim("alias", "Ли", MILANA, "2026-10-02")])
    assert cards[LILIAN]["gender"] is None
    assert cards[LILIAN]["name"] is None


@pytest.mark.parametrize("raw", ["m", "M", "мужчина", "муж"])
def test_gender_words_normalize_to_m(raw):
    assert normalize_claim("gender", raw) == ("gender", "m")


@pytest.mark.parametrize(
    ("field", "value"),
    [("gender", "x"), ("age", "30"), ("name", "  "), ("alias", "я" * 65)],
)
def test_bad_claims_are_rejected(field, value):
    with pytest.raises(ClaimError):
        normalize_claim(field, value)
