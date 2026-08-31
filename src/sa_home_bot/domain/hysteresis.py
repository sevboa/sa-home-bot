"""Чистое ядро анти-дребезга: (полоса, прошлое состояние) → новое + флаг перехода.

Общее для двух видов показаний — температурных (`domain/health.py`) и host-метрик
VPS (`domain/host.py`). Полосу (OVER/UNDER/MID) считает политика порогов вызывающей
стороны; здесь только гистерезис по счётчику подряд идущих срезов в нужной зоне.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.models import ALERTING, OK
from sa_home_bot.domain.policy import BAND_OVER, BAND_UNDER


@dataclass(frozen=True)
class BandReconcile:
    """Итог одного шага reconciliation по полосе."""

    status: str  # OK | ALERTING
    consecutive_count: int
    alerting_since: datetime | None
    transitioned_to: str | None  # ALERTING | OK | None — был ли зафиксирован переход


def reconcile_band(
    band: str,
    *,
    prev_status: str,
    prev_count: int,
    alerting_since: datetime | None,
    consecutive_to_alert: int,
    consecutive_to_clear: int,
    now: datetime,
) -> BandReconcile:
    """Прогнать полосу через гистерезисный КА.

    Из OK: счётчик копит подряд идущие OVER-срезы; по достижении
    ``consecutive_to_alert`` — переход в ALERTING. Из ALERTING: счётчик копит
    подряд идущие UNDER-срезы; по достижении ``consecutive_to_clear`` — возврат в
    OK. MID (мёртвая зона) и противоположная полоса сбрасывают счётчик.
    """
    if prev_status == OK:
        if band == BAND_OVER:
            count = prev_count + 1
            if count >= consecutive_to_alert:
                return BandReconcile(ALERTING, 0, now, ALERTING)
            return BandReconcile(OK, count, None, None)
        return BandReconcile(OK, 0, None, None)

    # prev_status == ALERTING
    if band == BAND_UNDER:
        count = prev_count + 1
        if count >= consecutive_to_clear:
            return BandReconcile(OK, 0, None, OK)
        return BandReconcile(ALERTING, count, alerting_since, None)
    return BandReconcile(ALERTING, 0, alerting_since, None)
