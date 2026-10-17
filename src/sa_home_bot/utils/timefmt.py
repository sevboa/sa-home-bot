from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400


def ceil_div(dividend: int, divisor: int) -> int:
    return -(-dividend // divisor)


def split_dhms(seconds: float) -> tuple[int, int, int, int]:
    total = int(max(0, seconds))
    days, rest = divmod(total, SECONDS_PER_DAY)
    hours, rest = divmod(rest, SECONDS_PER_HOUR)
    minutes, secs = divmod(rest, SECONDS_PER_MINUTE)
    return days, hours, minutes, secs


def format_dhms(
    seconds: float,
    *,
    days: str = "д",
    hours: str = "ч",
    minutes: str = "м",
    secs: str = "с",
) -> str:
    d, h, m, s = split_dhms(seconds)
    parts: list[str] = []
    if d:
        parts.append(f"{d}{days}")
    if h:
        parts.append(f"{h}{hours}")
    if m:
        parts.append(f"{m}{minutes}")
    if s or not parts:
        parts.append(f"{s}{secs}")
    return " ".join(parts)


def format_local(value: datetime | str, fmt: str, *, tolerant: bool = False) -> str:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else value
        return moment.astimezone().strftime(fmt)
    except ValueError:
        if tolerant and isinstance(value, str):
            return value
        raise


def zone_or_none(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
