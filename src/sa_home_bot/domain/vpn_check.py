"""Ядро reconciliation для проверок доступности VPN: результат одной
проверки (ok/fail) + известное состояние (node, server, transport, target)
→ новое состояние + переход, если он случился.

Анти-дребезг (гистерезис), по образцу domain/health.py, но булевый вместо
band OVER/MID/UNDER: переход в alerting фиксируется после N подряд
неудачных проверок, обратно в ok — после M подряд успешных. Чистая
функция, без БД/сети/asyncio — тестируется изолированно.

Ключ расширен этапом 39.0.7 (несколько VPN-серверов и транспортов, разбор
2026-09-18): ``node`` — наблюдатель (кто проверял), ``server`` — кого
проверяли, ``transport`` — каким транспортом (awg/reality). Один и тот же
``server``/``transport`` проверяется НЕСКОЛЬКИМИ наблюдателями независимо
— ``rollup_status`` ниже сводит их в единый статус для витрины (/vpn).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

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
    """Свести статусы нескольких наблюдателей об одном (server, transport)
    в один — для индикатора в /vpn. Вызывающий сам фильтрует записи до
    нужной пары (server, transport) перед вызовом — эта функция уже не
    знает про ключи, только про сами значения ``status``.

    OK — согласны все наблюдатели; ALERTING — тоже согласны все, но в
    другую сторону; иначе (есть и то, и то) — PARTIAL, «доступно частично»
    — оранжевый индикатор (решение владельца 2026-09-18)."""
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
    prev_status = known.status if known else OK
    prev_count = known.consecutive_count if known else 0
    alerting_since = known.alerting_since if known else None

    def state(status: str, count: int, since: datetime | None) -> CheckState:
        return CheckState(
            node=result.node,
            server=result.server,
            transport=result.transport,
            target=result.target,
            status=status,
            last_ok=result.ok,
            last_latency_ms=result.latency_ms,
            last_error=result.error,
            consecutive_count=count,
            alerting_since=since,
        )

    def transition(from_status: str, to_status: str) -> CheckTransition:
        return CheckTransition(
            node=result.node,
            server=result.server,
            transport=result.transport,
            target=result.target,
            from_status=from_status,
            to_status=to_status,
            at=now,
        )

    if prev_status == OK:
        # Счётчик копит подряд идущие неудачи.
        if not result.ok:
            count = prev_count + 1
            if count >= fail_threshold:
                return state(ALERTING, 0, now), transition(OK, ALERTING)
            return state(OK, count, None), None
        # Успех — серия неудач прервалась.
        return state(OK, 0, None), None

    # prev_status == ALERTING: счётчик копит подряд идущие успехи.
    if result.ok:
        count = prev_count + 1
        if count >= clear_threshold:
            return state(OK, 0, None), transition(ALERTING, OK)
        return state(ALERTING, count, alerting_since), None
    # Снова неудача — остаёмся в alerting, серия восстановления прервалась.
    return state(ALERTING, 0, alerting_since), None
