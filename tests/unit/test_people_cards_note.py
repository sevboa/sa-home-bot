"""Справка о людях из карточек (Этап 54.3, bot/people_cards.py)."""

from __future__ import annotations

from sa_home_bot.bot.people_cards import roster_line, speaker_card_note

NAMES = {202: "Милана"}


def name_of(person_id):
    return NAMES.get(person_id)


def _card(gender, by_id, strength, aliases=()):
    return {
        "gender": {"value": gender, "by_id": by_id, "strength": strength, "at": "1"},
        "name": None,
        "aliases": [{"value": a, "by_id": by_id, "strength": strength} for a in aliases],
    }


def test_weak_claim_names_its_source():
    note = speaker_card_note(_card("f", 202, "acquaintance"), name_of)
    assert "Собеседник — женщина: так говорит Милана" in note
    assert "назови источник: Милана" in note
    assert "«мадам»" in note


def test_self_claim_gender_inflects_aliases():
    note = speaker_card_note(_card("f", 303, "self", ["Лиля"]), name_of)
    assert "Собеседник — женщина: сказала это о себе сама" in note
    assert note.endswith("Её также зовут: Лиля.")


def test_empty_card_gives_no_note():
    assert speaker_card_note(None, name_of) is None
    assert speaker_card_note({"gender": None, "name": None, "aliases": []}, name_of) is None


def test_roster_line_without_card_is_name_and_id():
    assert roster_line(303, "Павел", None, name_of) == "Павел — id 303"


def test_unknown_source_falls_back_to_id():
    line = roster_line(303, "Павел", _card("m", 404, "acquaintance"), name_of)
    assert line == "Павел — id 303 — мужчина (так говорит гость с id 404)"
