from __future__ import annotations

from datetime import UTC, datetime, tzinfo

GB = 1_000_000_000

_MONTHS_NOMINATIVE = (
    "январь февраль март апрель май июнь июль август сентябрь октябрь ноябрь декабрь"
).split()
_MONTHS_GENITIVE = (
    "января февраля марта апреля мая июня июля августа сентября октября ноября декабря"
).split()


def bytes_to_gb(size: int) -> float:
    return size / GB


def format_gb(size: int) -> str:
    gb = bytes_to_gb(size)
    if size > 0 and gb < 0.05:
        return "меньше 0.1 ГБ"
    return f"{gb:.1f} ГБ"


def format_gb_short(size: int) -> str:
    gb = bytes_to_gb(size)
    return f"{gb:.0f}" if gb >= 10 else f"{gb:.1f}".removesuffix(".0")


def format_stamp(stamp: str | None, *, tz: tzinfo | None = None, with_time: bool = True) -> str:
    if not stamp:
        return ""
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return stamp[:16].replace("T", " ")
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(tz)
    return when.strftime("%d.%m %H:%M" if with_time else "%d.%m")


def next_reset_phrase(now: datetime) -> str:
    return f"1 {_MONTHS_GENITIVE[now.month % 12]}"


def month_name(now: datetime) -> str:
    return _MONTHS_NOMINATIVE[now.month - 1]
