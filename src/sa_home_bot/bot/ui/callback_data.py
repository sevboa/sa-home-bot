from __future__ import annotations

SEPARATOR = ":"


def pack(prefix: str, *parts: object) -> str:
    return SEPARATOR.join([prefix, *(str(part) for part in parts)])


def unpack(data: str | None, prefix: str, parts: int) -> tuple[str, ...] | None:
    if not data:
        return None
    pieces = data.split(SEPARATOR)
    if len(pieces) != parts + 1 or pieces[0] != prefix:
        return None
    return tuple(pieces[1:])


def unpack_id_and_button(data: str | None, prefix: str) -> tuple[int, str] | None:
    pieces = unpack(data, prefix, 2)
    if pieces is None or not pieces[0].isdigit():
        return None
    return int(pieces[0]), pieces[1]


def int_or(raw: str | None, default: int = 0) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def part(parts: list[str], index: int) -> str | None:
    return parts[index] if len(parts) > index else None
