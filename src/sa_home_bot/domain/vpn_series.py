from __future__ import annotations

from datetime import datetime

from sa_home_bot.domain.hysteresis import BandReconcile, reconcile_band
from sa_home_bot.domain.models import OK
from sa_home_bot.domain.policy import BAND_OVER, BAND_UNDER
from sa_home_bot.domain.vpn_check import KnownCheckState

VERDICT_OK = "ok"
BROKEN_SERIES_VERDICTS = ("freeze", "cut", "error")


def failure_at_bytes(series: list[dict]) -> int | None:
    for item in series:
        if isinstance(item, dict) and item.get("verdict") in BROKEN_SERIES_VERDICTS:
            value = item.get("stalled_at_bytes")
            if value is None:
                value = item.get("bytes_total")
            return int(value) if isinstance(value, int | float) else None
    return None


def series_verdict(raw_verdict: str, series_verdicts: list[str]) -> str:
    if raw_verdict != VERDICT_OK:
        return raw_verdict
    return next((v for v in series_verdicts if v in BROKEN_SERIES_VERDICTS), VERDICT_OK)


def reconcile_vpn_series(
    verdict: str,
    known: KnownCheckState | None,
    now: datetime,
    *,
    fail_threshold: int,
    clear_threshold: int,
) -> BandReconcile:
    return reconcile_band(
        BAND_UNDER if verdict == VERDICT_OK else BAND_OVER,
        prev_status=known.status if known else OK,
        prev_count=known.consecutive_count if known else 0,
        alerting_since=known.alerting_since if known else None,
        consecutive_to_alert=fail_threshold,
        consecutive_to_clear=clear_threshold,
        now=now,
    )
