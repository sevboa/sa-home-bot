"""strip_math_notation — зачистка LaTeX-разметки формул из ответа модели.

Живая находка 2026-07-24: чисто промптовая инструкция ненадёжна (см.
llm/prompt.py docstring) — чистим в коде после ответа модели, не полагаясь
на то, что модель сама будет соблюдать формат. Картавость (раньше здесь же
— apply_speech_defect) теперь вероятностная, излечимая механика «Логопед»
— см. tests/unit/test_speech_therapy.py."""

from __future__ import annotations

from sa_home_bot.llm.prompt import strip_math_notation

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
