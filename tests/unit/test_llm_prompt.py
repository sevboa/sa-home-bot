from __future__ import annotations

import pytest

from sa_home_bot.llm import prompt
from sa_home_bot.llm.prompt import strip_math_notation, wrap_context_note, wrap_system_directive

PLAIN_TEXT = (
    "Площадь поверхности цилиндра составляет приблизительно 33 квадратных метра."
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("площадь равна $10.5$ м2", "площадь равна 10.5 м2"),
        ("r^2", "r²"),
        ("x^{10}", "x¹⁰"),
        (r"\frac{1}{2}", "(1)/(2)"),
        (r"\sqrt{2}", "√(2)"),
        (r"\text{метров}", "метров"),
        (PLAIN_TEXT, PLAIN_TEXT),
    ],
)
def test_strip_math_converts_notation(raw, expected):
    assert strip_math_notation(raw) == expected


def test_strip_math_replaces_pi_times_and_approx():
    result = strip_math_notation(r"$2 \pi r^2 + 2 \pi r h$, \approx 32.99")
    assert "\\" not in result
    assert "π" in result
    assert "≈" in result


def test_strip_math_strips_unknown_commands_and_braces():
    result = strip_math_notation(r"\alpha {что-то}")
    assert "\\" not in result
    assert "{" not in result and "}" not in result


def test_strip_math_full_cylinder_example_has_no_leftover_latex():
    raw = (
        r"Площадь поверхности цилиндра равна $2 \pi r (r + h)$, где $r$ — радиус, "
        r"$h$ — высота. Подставляя значения, получаем $2 \pi (1.5)(1.5 + 2) "
        r"\approx 32.99$ квадратных метров."
    )
    result = strip_math_notation(raw)
    for token in ("$", "\\pi", "\\approx", "\\times", "{", "}"):
        assert token not in result


def test_wrap_context_note_returns_system_role_and_body_unchanged():
    assert wrap_context_note("hello") == {"role": "system", "content": "hello"}


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
