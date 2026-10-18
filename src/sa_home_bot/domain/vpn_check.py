from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sa_home_bot.domain.hysteresis import reconcile_band
from sa_home_bot.domain.policy import BAND_OVER, BAND_UNDER

OK = "ok"
ALERTING = "alerting"
PARTIAL = "partial"


@dataclass(frozen=True)
class CheckResult:
    node: str
    server: str
    transport: str
    target: str
    ok: bool
    latency_ms: int | None
    error: str | None


@dataclass(frozen=True)
class KnownCheckState:
    status: str
    consecutive_count: int
    alerting_since: datetime | None


@dataclass(frozen=True)
class CheckState:
    node: str
    server: str
    transport: str
    target: str
    status: str
    last_ok: bool
    last_latency_ms: int | None
    last_error: str | None
    consecutive_count: int
    alerting_since: datetime | None


@dataclass(frozen=True)
class CheckTransition:
    node: str
    server: str
    transport: str
    target: str
    from_status: str
    to_status: str
    at: datetime


def rollup_status(statuses: Iterable[str]) -> str:
    values = list(statuses)
    if not values:
        raise ValueError("statuses не должен быть пустым — нечего сводить")
    uniq = set(values)
    if uniq == {OK}:
        return OK
    if uniq == {ALERTING}:
        return ALERTING
    return PARTIAL


def reconcile_vpn_check(
    result: CheckResult,
    known: KnownCheckState | None,
    now: datetime,
    *,
    fail_threshold: int,
    clear_threshold: int,
) -> tuple[CheckState, CheckTransition | None]:
    band = BAND_UNDER if result.ok else BAND_OVER
    step = reconcile_band(
        band,
        prev_status=known.status if known else OK,
        prev_count=known.consecutive_count if known else 0,
        alerting_since=known.alerting_since if known else None,
        consecutive_to_alert=fail_threshold,
        consecutive_to_clear=clear_threshold,
        now=now,
    )
    state = CheckState(
        node=result.node,
        server=result.server,
        transport=result.transport,
        target=result.target,
        status=step.status,
        last_ok=result.ok,
        last_latency_ms=result.latency_ms,
        last_error=result.error,
        consecutive_count=step.consecutive_count,
        alerting_since=step.alerting_since,
    )
    if step.transitioned_to is None:
        return state, None
    transition = CheckTransition(
        node=result.node,
        server=result.server,
        transport=result.transport,
        target=result.target,
        from_status=OK if step.transitioned_to == ALERTING else ALERTING,
        to_status=step.transitioned_to,
        at=now,
    )
    return state, transition
