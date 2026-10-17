from __future__ import annotations

import json
from typing import Any


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    return toml_string(str(value))


def toml_table(name: str, values: dict[str, Any]) -> str:
    body = [f"{k} = {toml_value(v)}" for k, v in values.items() if v is not None]
    return "\n".join([f"[{name}]", *body]) + "\n"
