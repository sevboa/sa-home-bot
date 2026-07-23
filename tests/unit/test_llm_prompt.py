"""apply_speech_defect — детерминированная картавость Альфреда (р→г).

Живая находка 2026-07-24: чисто промптовая инструкция ненадёжна (см.
llm/prompt.py docstring) — заменяем в коде после ответа модели."""

from __future__ import annotations

from sa_home_bot.llm.prompt import apply_speech_defect


def test_replaces_lowercase_and_uppercase_r():
    assert apply_speech_defect("сэр Роман") == "сэг Гоман"


def test_replaces_every_occurrence_in_a_word():
    assert apply_speech_defect("Температура") == "Темпегатуга"


def test_is_idempotent_does_not_touch_already_substituted_g():
    once = apply_speech_defect("сэр")
    assert apply_speech_defect(once) == once


def test_replaces_in_common_greeting_words():
    assert apply_speech_defect("добрый день") == "добгый день"
    assert apply_speech_defect("здравствуйте") == "здгавствуйте"
    assert apply_speech_defect("хорошего дня") == "хогошего дня"


def test_leaves_non_cyrillic_text_unchanged():
    assert apply_speech_defect("Hello, world! 123") == "Hello, world! 123"
