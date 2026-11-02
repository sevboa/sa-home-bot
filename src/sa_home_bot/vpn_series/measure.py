from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel

Verdict = Literal["ok", "freeze", "cut", "slow", "error"]

Sample = tuple[float, int]

CUT_EXIT_CODES = frozenset({18, 56})
TOLERATED_EXIT_CODES = frozenset({0, 28})
CUT_MIN_BYTES = 10 * 1024
CUT_MAX_BYTES = 30 * 1024


class SeriesResult(BaseModel):
    bytes_total: int
    duration_s: float
    stalled_at_bytes: int | None
    max_idle_s: float
    kbps: float
    verdict: Verdict


def _idle_stats(samples: Sequence[Sample]) -> tuple[float, int | None, int | None]:
    max_idle = 0.0
    stall_bytes: int | None = None
    peak = 0
    progress_t: float | None = None
    for t, received in samples:
        if received > peak:
            peak = received
            progress_t = t
            continue
        if progress_t is None:
            continue
        idle = t - progress_t
        if idle > max_idle:
            max_idle = idle
            stall_bytes = peak
    return max_idle, stall_bytes, peak


def classify(
    samples: Sequence[Sample],
    *,
    freeze_idle_s: float,
    min_kbps: float,
    curl_exit: int | None = None,
) -> SeriesResult:
    if not samples:
        return SeriesResult(
            bytes_total=0,
            duration_s=0.0,
            stalled_at_bytes=None,
            max_idle_s=0.0,
            kbps=0.0,
            verdict="error",
        )
    ordered = sorted(samples, key=lambda sample: sample[0])
    started = ordered[0][0]
    duration = ordered[-1][0] - started
    relative = [(t - started, received) for t, received in ordered]
    max_idle, stall_bytes, total = _idle_stats(relative)
    kbps = total * 8 / 1000 / duration if duration > 0 else 0.0

    stalled_at: int | None = None
    if curl_exit in CUT_EXIT_CODES and CUT_MIN_BYTES <= total <= CUT_MAX_BYTES:
        verdict: Verdict = "cut"
        stalled_at = total
    elif total == 0 or (curl_exit is not None and curl_exit not in TOLERATED_EXIT_CODES):
        verdict = "error"
    elif max_idle >= freeze_idle_s:
        verdict = "freeze"
        stalled_at = stall_bytes
    elif kbps < min_kbps:
        verdict = "slow"
    else:
        verdict = "ok"
    return SeriesResult(
        bytes_total=total,
        duration_s=duration,
        stalled_at_bytes=stalled_at,
        max_idle_s=max_idle,
        kbps=kbps,
        verdict=verdict,
    )
