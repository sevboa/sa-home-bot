from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MIN_INTERVAL = timedelta(seconds=60)
MAX_PER_DAY = 5
WINDOW = timedelta(days=1)


@dataclass(frozen=True)
class ScanDecision:
    allowed: bool
    reason: str
    ticks: tuple[datetime, ...]


def _mins(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60) + 1


def decide(ticks: list[datetime], now: datetime) -> ScanDecision:
    recent = sorted(t for t in ticks if now - t < WINDOW)

    if recent and now - recent[-1] < MIN_INTERVAL:
        wait = int((MIN_INTERVAL - (now - recent[-1])).total_seconds()) + 1
        return ScanDecision(False, f"⏳ Слишком часто — подождите ~{wait} с.", tuple(recent))

    if len(recent) >= MAX_PER_DAY:
        free_in = _mins(recent[0] + WINDOW - now)
        return ScanDecision(
            False,
            f"🚫 Лимит {MAX_PER_DAY} ручных сканов за сутки исчерпан. "
            f"Следующий — через ~{free_in} мин.",
            tuple(recent),
        )

    return ScanDecision(True, "", (*recent, now)[-MAX_PER_DAY:])
