from __future__ import annotations

import ast
import math
import operator
from collections.abc import Callable
from typing import Any

from sa_home_bot.bot.tools.core import ToolContext, ToolSpec

_ALLOWED_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_ALLOWED_NAMES: dict[str, float] = {"pi": math.pi, "e": math.e}
_MAX_POW_EXPONENT = 1000
_ROUND_NDIGITS = 6


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        left, right = _safe_eval(node.left), _safe_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_POW_EXPONENT:
            raise ValueError("слишком большая степень")
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError(
        "недопустимое выражение (разрешены только числа, pi, e и + - * / ** или ^ ())"
    )


async def tool_calc(ctx: ToolContext, args: dict[str, Any]) -> str:
    expr = args.get("expression")
    if not isinstance(expr, str) or not expr.strip():
        return "ошибка: пустое выражение"
    expr = expr.replace("^", "**")
    try:
        tree = ast.parse(expr, mode="eval")
        value = _safe_eval(tree.body)
    except (SyntaxError, ValueError, ZeroDivisionError, TypeError, OverflowError) as exc:
        return f"ошибка вычисления: {exc}"
    if isinstance(value, float):
        if value.is_integer():
            value = int(value)
        else:
            value = round(value, _ROUND_NDIGITS)
    return str(value)


_DECL_CALC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "calc",
        "description": (
            "Точно вычислить арифметическое выражение (числа, + - * / скобки, "
            "степень как ** или ^, плюс константы pi и e — без произвольных "
            "переменных и функций). Используй для ЛЮБОЙ реальной арифметики, "
            "включая формулы (площадь, объём и т.п.) — подставь известные "
            "числа и pi/e в выражение и вызови тул, не считай и не "
            "подставляй в уме."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Например: 2 * pi * 1.5 * (1.5 + 2) или 1.5^2",
                }
            },
            "required": ["expression"],
        },
    },
}


CALC = ToolSpec(name="calc", handler=tool_calc, declaration=_DECL_CALC)
