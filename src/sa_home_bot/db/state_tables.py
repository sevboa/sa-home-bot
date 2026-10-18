from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sa_home_bot.domain.host import HostMetricState
from sa_home_bot.domain.models import ALERTING, OK, HealthState


def iso_or_none(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def parse_iso_or_none(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class StateTable:
    table: str
    immutable_column: str
    mutable_columns: tuple[tuple[str, str], ...]
    row_to_state: Callable[[Any], Any]
    prune_scoped_by_immutable: bool
    restamp_alerting_since_on_start: bool

    async def apply(self, conn, states: Iterable, transitions: Iterable, now: datetime) -> None:
        states = list(states)
        if not states:
            return
        now_s = iso_or_none(now)
        started = {t.component_id for t in transitions if t.to_status == ALERTING}
        cleared = {t.component_id for t in transitions if t.to_status == OK}
        names = [column for column, _ in self.mutable_columns]
        assignments = ", ".join(f"{column}=?" for column in names)
        for st in states:
            values = tuple(getattr(st, attr) for _, attr in self.mutable_columns)
            since_s = iso_or_none(st.alerting_since)
            cur = await conn.execute(
                f"SELECT 1 FROM {self.table} WHERE component_id=?", (st.component_id,)
            )
            if await cur.fetchone() is None:
                columns = ", ".join(
                    ["component_id", self.immutable_column, *names]
                    + ["alerting_since", "first_seen_at", "last_seen_at"]
                )
                marks = ", ".join("?" for _ in range(len(names) + 5))
                await conn.execute(
                    f"INSERT INTO {self.table}({columns}) VALUES({marks})",
                    (
                        st.component_id,
                        getattr(st, self.immutable_column),
                        *values,
                        since_s,
                        now_s,
                        now_s,
                    ),
                )
                continue
            if st.component_id in started:
                since = now_s if self.restamp_alerting_since_on_start else since_s
                tail = ", notified_alert_at=NULL, notified_cleared_at=NULL"
                since_sql = "?"
            elif st.component_id in cleared:
                since, tail, since_sql = None, ", notified_cleared_at=NULL", "NULL"
            else:
                since, tail, since_sql = since_s, "", "?"
            params = (*values, *([since] if since_sql == "?" else []), now_s, st.component_id)
            await conn.execute(
                f"UPDATE {self.table} SET {assignments}, alerting_since={since_sql}, "
                f"last_seen_at=?{tail} WHERE component_id=?",
                params,
            )
        await self._prune_missing(conn, states)

    async def _prune_missing(self, conn, states: list) -> None:
        current_ids = tuple({st.component_id for st in states})
        id_marks = ",".join("?" for _ in current_ids)
        if not self.prune_scoped_by_immutable:
            await conn.execute(
                f"DELETE FROM {self.table} WHERE component_id NOT IN ({id_marks})", current_ids
            )
            return
        scanned = tuple({getattr(st, self.immutable_column) for st in states})
        scope_marks = ",".join("?" for _ in scanned)
        await conn.execute(
            f"DELETE FROM {self.table} WHERE {self.immutable_column} IN ({scope_marks}) "
            f"AND component_id NOT IN ({id_marks})",
            (*scanned, *current_ids),
        )


def _row_to_health_state(row) -> HealthState:
    return HealthState(
        component_id=row["component_id"],
        kind=row["kind"],
        label=row["label"],
        status=row["status"],
        temperature_c=row["last_temperature_c"],
        consecutive_count=row["consecutive_count"],
        alerting_since=parse_iso_or_none(row["alerting_since"]),
    )


def _row_to_host_state(row) -> HostMetricState:
    return HostMetricState(
        component_id=row["component_id"],
        metric=row["metric"],
        label=row["label"],
        value=row["last_value"],
        unit=row["unit"],
        status=row["status"],
        consecutive_count=row["consecutive_count"],
        alerting_since=parse_iso_or_none(row["alerting_since"]),
    )


HEALTH_STATES = StateTable(
    table="health_states",
    immutable_column="kind",
    mutable_columns=(
        ("status", "status"),
        ("label", "label"),
        ("last_temperature_c", "temperature_c"),
        ("consecutive_count", "consecutive_count"),
    ),
    row_to_state=_row_to_health_state,
    prune_scoped_by_immutable=True,
    restamp_alerting_since_on_start=True,
)

HOST_STATES = StateTable(
    table="host_metric_states",
    immutable_column="metric",
    mutable_columns=(
        ("status", "status"),
        ("label", "label"),
        ("unit", "unit"),
        ("last_value", "value"),
        ("consecutive_count", "consecutive_count"),
    ),
    row_to_state=_row_to_host_state,
    prune_scoped_by_immutable=False,
    restamp_alerting_since_on_start=False,
)
