"""strip_math_notation — зачистка LaTeX-разметки формул из ответа модели.

Живая находка 2026-07-24: чисто промптовая инструкция ненадёжна (см.
llm/prompt.py docstring) — чистим в коде после ответа модели, не полагаясь
на то, что модель сама будет соблюдать формат. Картавость (раньше здесь же
— apply_speech_defect) теперь вероятностная, излечимая механика «Логопед»
— см. tests/unit/test_speech_therapy.py.

wrap_context_note/wrap_system_directive — два хелпера для сборки
messages-словарей (framing-заметка vs системная директива), см. докстринги
в llm/prompt.py. Отдельная проверка на регрессию: у них НЕ общая роль —
у прежней версии дизайна была одна _DIRECTIVE_ROLE на оба хелпера, из-за
чего смена роли для директив тихо поменяла бы и роль context_note."""

from __future__ import annotations

from sa_home_bot.llm import prompt
from sa_home_bot.llm.prompt import strip_math_notation, wrap_context_note, wrap_system_directive

# --- strip_math_notation ---


def test_strip_math_strips_dollar_delimiters():
    assert strip_math_notation("площадь равна $10.5$ м2") == "площадь равна 10.5 м2"


def test_strip_math_replaces_pi_times_and_approx():
    result = strip_math_notation(r"$2 \pi r^2 + 2 \pi r h$, \approx 32.99")
    assert "\\" not in result
    assert "π" in result
    assert "≈" in result


def test_strip_math_converts_caret_exponent_to_superscript():
    assert strip_math_notation("r^2") == "r²"
    assert strip_math_notation("x^{10}") == "x¹⁰"


def test_strip_math_converts_frac_to_slash():
    assert strip_math_notation(r"\frac{1}{2}") == "(1)/(2)"


def test_strip_math_converts_sqrt():
    assert strip_math_notation(r"\sqrt{2}") == "√(2)"


def test_strip_math_unwraps_text_command():
    assert strip_math_notation(r"\text{метров}") == "метров"


def test_strip_math_strips_unknown_commands_and_braces():
    result = strip_math_notation(r"\alpha {что-то}")
    assert "\\" not in result
    assert "{" not in result and "}" not in result


def test_strip_math_leaves_plain_russian_text_unchanged():
    text = "Площадь поверхности цилиндра составляет приблизительно 33 квадратных метра."
    assert strip_math_notation(text) == text


def test_strip_math_full_cylinder_example_has_no_leftover_latex():
    raw = (
        r"Площадь поверхности цилиндра равна $2 \pi r (r + h)$, где $r$ — радиус, "
        r"$h$ — высота. Подставляя значения, получаем $2 \pi (1.5)(1.5 + 2) "
        r"\approx 32.99$ квадратных метров."
    )
    result = strip_math_notation(raw)
    for token in ("$", "\\pi", "\\approx", "\\times", "{", "}"):
        assert token not in result


# --- wrap_context_note / wrap_system_directive ---


def test_wrap_context_note_returns_system_role_and_body_unchanged():
    assert wrap_context_note("hello") == {"role": "system", "content": "hello"}


def test_wrap_context_note_role_is_system_even_if_directive_role_changes():
    # Регрессия: раньше обе функции делили одну role-константу — смена роли
    # директив тихо меняла бы и role заметки. wrap_context_note role="system"
    # захардкожена независимо от _DIRECTIVE_ROLE (см. докстринг в llm/prompt.py).
    original = prompt._DIRECTIVE_ROLE
    try:
        prompt._DIRECTIVE_ROLE = "system_OTHER"
        assert wrap_context_note("hello")["role"] == "system"
    finally:
        prompt._DIRECTIVE_ROLE = original


def test_wrap_system_directive_prepends_marker_and_uses_directive_role():
    result = wrap_system_directive("hello")
    assert result == {
        "role": prompt._DIRECTIVE_ROLE,
        "content": prompt._DIRECTIVE_MARKER + "hello",
    }


def test_directive_marker_is_nonempty_and_contains_guard_language():
    assert isinstance(prompt._DIRECTIVE_MARKER, str)
    assert prompt._DIRECTIVE_MARKER
    assert "СИСТЕМНАЯ ДИРЕКТИВА" in prompt._DIRECTIVE_MARKER
    assert "Принято" in prompt._DIRECTIVE_MARKER
