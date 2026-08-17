"""Ядро reconciliation для проверок доступности VPN: результат одной
проверки (ok/fail) + известное состояние (node, target) → новое состояние
+ переход, если он случился.

Анти-дребезг (гистерезис), по образцу domain/health.py, но булевый вместо
band OVER/MID/UNDER: переход в alerting фиксируется после N подряд
неудачных проверок, обратно в ok — после M подряд успешных. Чистая
функция, без БД/сети/asyncio — тестируется изолированно.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

OK = "ok"
ALERTING = "alerting"


@dataclass(frozen=True)
class CheckResult:
    node: str
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
    target: str
    from_status: str
    to_status: str
    at: datetime


def reconcile_vpn_check(
    result: CheckResult,
    known: KnownCheckState | None,
    now: datetime,
    *,
    fail_threshold: int,
    clear_threshold: int,
) -> tuple[CheckState, CheckTransition | None]:
    prev_status = known.status if known else OK
    prev_count = known.consecutive_count if known else 0
    alerting_since = known.alerting_since if known else None

    def state(status: str, count: int, since: datetime | None) -> CheckState:
        return CheckState(
            node=result.node,
            target=result.target,
            status=status,
            last_ok=result.ok,
            last_latency_ms=result.latency_ms,
            last_error=result.error,
            consecutive_count=count,
            alerting_since=since,
        )

    if prev_status == OK:
        # Счётчик копит подряд идущие неудачи.
        if not result.ok:
            count = prev_count + 1
            if count >= fail_threshold:
                transition = CheckTransition(
                    node=result.node,
                    target=result.target,
                    from_status=OK,
                    to_status=ALERTING,
                    at=now,
                )
                return state(ALERTING, 0, now), transition
            return state(OK, count, None), None
        # Успех — серия неудач прервалась.
        return state(OK, 0, None), None

    # prev_status == ALERTING: счётчик копит подряд идущие успехи.
    if result.ok:
        count = prev_count + 1
        if count >= clear_threshold:
            transition = CheckTransition(
                node=result.node,
                target=result.target,
                from_status=ALERTING,
                to_status=OK,
                at=now,
            )
            return state(OK, 0, None), transition
        return state(ALERTING, count, alerting_since), None
    # Снова неудача — остаёмся в alerting, серия восстановления прервалась.
    return state(ALERTING, 0, alerting_since), None
