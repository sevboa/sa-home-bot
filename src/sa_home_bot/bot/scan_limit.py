"""Лимит ручного форс-скана: не чаще раза в минуту и не более N за сутки.

Чистая логика решения (без БД) — счётчик хранится в app_state как список меток
времени принятых сканов; персист делает Store. Окно суточного лимита —
скользящее (последние 24 ч), чтобы нельзя было обойти сбросом в полночь.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

MIN_INTERVAL = timedelta(seconds=60)  # не чаще раза в минуту
MAX_PER_DAY = 5  # не более 5 за скользящие сутки
WINDOW = timedelta(days=1)


@dataclass(frozen=True)
class ScanDecision:
    allowed: bool
    reason: str  # текст для пользователя при отказе ("" если allowed)
    ticks: tuple[datetime, ...]  # метки для сохранения (при allowed — с добавленной now)


def _mins(delta: timedelta) -> int:
    return int(delta.total_seconds() // 60) + 1


def decide(ticks: list[datetime], now: datetime) -> ScanDecision:
    """Решить, можно ли запустить ручной скан сейчас.

    ``ticks`` — метки прошлых принятых сканов из БД. При отказе ``ticks`` в
    ответе — очищенный от старых список (для перезаписи), новую метку НЕ
    добавляем. При разрешении — список с добавленной ``now`` (не длиннее лимита).
    """
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
