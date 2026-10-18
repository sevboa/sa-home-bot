from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.models import ALERTING, OK
from sa_home_bot.domain.policy import BAND_OVER, BAND_UNDER


@dataclass(frozen=True)
class BandReconcile:
    status: str
    consecutive_count: int
    alerting_since: datetime | None
    transitioned_to: str | None


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
    if prev_status == OK:
        if band == BAND_OVER:
            count = prev_count + 1
            if count >= consecutive_to_alert:
                return BandReconcile(ALERTING, 0, now, ALERTING)
            return BandReconcile(OK, count, None, None)
        return BandReconcile(OK, 0, None, None)

    if band == BAND_UNDER:
        count = prev_count + 1
        if count >= consecutive_to_clear:
            return BandReconcile(OK, 0, None, OK)
        return BandReconcile(ALERTING, count, alerting_since, None)
    return BandReconcile(ALERTING, 0, alerting_since, None)
