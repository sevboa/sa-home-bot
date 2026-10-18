from __future__ import annotations

import math

import pytest

from sa_home_bot.bot import tools
from tests.unit.tools_support import make_ctx


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 2", "4"),
        ("(2 + 3) * 4", "20"),
        ("7 / 2", "3.5"),
        ("2 ** 10", "1024"),
        ("2 * pi", "6.283185"),
        ("e", "2.718282"),
        ("1 / 3", "0.333333"),
        ("1.5^2", "2.25"),
        (
            "2 * pi * 1.5^2 + 2 * pi * 1.5 * 2",
            str(round(2 * math.pi * 1.5**2 + 2 * math.pi * 1.5 * 2, 6)),
        ),
        ("2 * pi * 1.5 * (1.5 + 2)", "32.986723"),
    ],
)
async def test_calc_evaluates(expression, expected):
    assert await tools.tool_calc(make_ctx(None), {"expression": expression}) == expected


@pytest.mark.parametrize(
    "args",
    [
        {"expression": "x + 1"},
        {"expression": "__import__('os').system('ls')"},
        {"expression": "1 / 0"},
        {"expression": "2 ** 100000"},
        {},
    ],
)
async def test_calc_rejects(args):
    assert (await tools.tool_calc(make_ctx(None), args)).startswith("ошибка")


async def test_calc_rejects_empty_expression():
    result = await tools.tool_calc(make_ctx(None), {"expression": ""})
    assert result == "ошибка: пустое выражение"
