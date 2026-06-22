"""Store — единственный слой доступа к БД. Голый SQL, без ORM.

Идемпотентность уведомлений: флаги `notified_alert_at` / `notified_cleared_at`
выставляются только после успешной отправки. Жизненный цикл одного цикла
перегрева: переход в alerting сбрасывает оба флага; отправка "перегрев"
выставляет `notified_alert_at`; переход в ok сбрасывает `notified_cleared_at`;
отправка "остыл" выставляет его.
"""

from __future__ import annotations

from datetime import datetime

from sentinel_bot.db.connection import Database
from sentinel_bot.domain.models import (
    ALERTING,
    OK,
    HealthDiff,
    HealthState,
    KnownState,
)

NOTIF_ALERT = "alert"
NOTIF_CLEARED = "cleared"


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_state(row) -> HealthState:
    return HealthState(
        component_id=row["component_id"],
        kind=row["kind"],
        label=row["label"],
        status=row["status"],
        temperature_c=row["last_temperature_c"],
        consecutive_count=row["consecutive_count"],
        alerting_since=_parse(row["alerting_since"]),
    )


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- app_state (KV) ---

    async def get_state(self, key: str) -> str | None:
        cur = await self.db.conn.execute("SELECT value FROM app_state WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_state(self, key: str, value: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO app_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # --- job_runs ---

    async def start_job_run(self, job_type: str, started_at: datetime) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO job_runs(job_type, started_at, status) VALUES(?, ?, 'running')",
                (job_type, _iso(started_at)),
            )
            return cur.lastrowid

    async def finish_job_run(
        self,
        run_id: int,
        status: str,
        finished_at: datetime,
        error: str | None = None,
        metrics_json: str | None = None,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE job_runs SET status=?, finished_at=?, error=?, metrics_json=? WHERE id=?",
                (status, _iso(finished_at), error, metrics_json, run_id),
            )

    async def recent_job_runs(self, limit: int = 10) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def job_run_counts(self) -> dict[str, int]:
        cur = await self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM job_runs GROUP BY status"
        )
        rows = await cur.fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- health_states ---

    async def get_known_states(self) -> dict[str, KnownState]:
        cur = await self.db.conn.execute(
            "SELECT component_id, status, consecutive_count, alerting_since FROM health_states"
        )
        rows = await cur.fetchall()
        return {
            r["component_id"]: KnownState(
                component_id=r["component_id"],
                status=r["status"],
                consecutive_count=r["consecutive_count"],
                alerting_since=_parse(r["alerting_since"]),
            )
            for r in rows
        }

    async def get_all_states(self) -> list[HealthState]:
        cur = await self.db.conn.execute(
            "SELECT * FROM health_states ORDER BY kind, component_id"
        )
        rows = await cur.fetchall()
        return [_row_to_state(r) for r in rows]

    async def apply_diff(self, diff: HealthDiff, now: datetime) -> None:
        """Одной транзакцией записать новый срез состояний и сбросить флаги по переходам."""
        now_s = _iso(now)
        started = {t.component_id for t in diff.transitions if t.to_status == ALERTING}
        cleared = {t.component_id for t in diff.transitions if t.to_status == OK}

        async with self.db.transaction() as conn:
            for st in diff.states:
                alerting_since_s = _iso(st.alerting_since)
                cur = await conn.execute(
                    "SELECT 1 FROM health_states WHERE component_id=?", (st.component_id,)
                )
                exists = await cur.fetchone() is not None

                if not exists:
                    await conn.execute(
                        "INSERT INTO health_states("
                        "component_id, kind, label, status, last_temperature_c, "
                        "consecutive_count, alerting_since, first_seen_at, last_seen_at, "
                        "notified_alert_at, notified_cleared_at) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                        (
                            st.component_id,
                            st.kind,
                            st.label,
                            st.status,
                            st.temperature_c,
                            st.consecutive_count,
                            alerting_since_s,
                            now_s,
                            now_s,
                        ),
                    )
                elif st.component_id in started:
                    # Новый цикл перегрева — сбрасываем оба флага доставки.
                    await conn.execute(
                        "UPDATE health_states SET status=?, label=?, last_temperature_c=?, "
                        "consecutive_count=?, alerting_since=?, last_seen_at=?, "
                        "notified_alert_at=NULL, notified_cleared_at=NULL "
                        "WHERE component_id=?",
                        (
                            st.status,
                            st.label,
                            st.temperature_c,
                            st.consecutive_count,
                            now_s,
                            now_s,
                            st.component_id,
                        ),
                    )
                elif st.component_id in cleared:
                    # Остыл — нужно разослать "остыл" (флаг alert сохраняем).
                    await conn.execute(
                        "UPDATE health_states SET status=?, label=?, last_temperature_c=?, "
                        "consecutive_count=?, alerting_since=NULL, last_seen_at=?, "
                        "notified_cleared_at=NULL WHERE component_id=?",
                        (
                            st.status,
                            st.label,
                            st.temperature_c,
                            st.consecutive_count,
                            now_s,
                            st.component_id,
                        ),
                    )
                else:
                    # Без перехода — флаги доставки не трогаем.
                    await conn.execute(
                        "UPDATE health_states SET status=?, label=?, last_temperature_c=?, "
                        "consecutive_count=?, alerting_since=?, last_seen_at=? "
                        "WHERE component_id=?",
                        (
                            st.status,
                            st.label,
                            st.temperature_c,
                            st.consecutive_count,
                            alerting_since_s,
                            now_s,
                            st.component_id,
                        ),
                    )

    # --- pending уведомления ---

    async def pending_alerts(self) -> list[HealthState]:
        cur = await self.db.conn.execute(
            "SELECT * FROM health_states WHERE status='alerting' AND notified_alert_at IS NULL"
        )
        rows = await cur.fetchall()
        return [_row_to_state(r) for r in rows]

    async def pending_clears(self) -> list[HealthState]:
        cur = await self.db.conn.execute(
            "SELECT * FROM health_states WHERE status='ok' "
            "AND notified_alert_at IS NOT NULL AND notified_cleared_at IS NULL"
        )
        rows = await cur.fetchall()
        return [_row_to_state(r) for r in rows]

    async def mark_alert_notified(self, component_id: str, at: datetime) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE health_states SET notified_alert_at=? WHERE component_id=?",
                (_iso(at), component_id),
            )

    async def mark_cleared_notified(self, component_id: str, at: datetime) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE health_states SET notified_cleared_at=? WHERE component_id=?",
                (_iso(at), component_id),
            )

    # --- health_notifications (для reply-цепочки) ---

    async def record_notification(
        self,
        component_id: str,
        chat_id: int,
        kind: str,
        message_id: int | None,
        sent_at: datetime,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO health_notifications("
                "component_id, chat_id, kind, message_id, sent_at) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(component_id, chat_id, kind) DO UPDATE SET "
                "message_id=excluded.message_id, sent_at=excluded.sent_at",
                (component_id, chat_id, kind, message_id, _iso(sent_at)),
            )

    async def get_alert_message_id(self, component_id: str, chat_id: int) -> int | None:
        cur = await self.db.conn.execute(
            "SELECT message_id FROM health_notifications "
            "WHERE component_id=? AND chat_id=? AND kind=?",
            (component_id, chat_id, NOTIF_ALERT),
        )
        row = await cur.fetchone()
        return row["message_id"] if row else None

    # --- housekeeping ---

    async def prune_job_runs(self, keep_last: int = 500) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM job_runs WHERE id NOT IN "
                "(SELECT id FROM job_runs ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
            return cur.rowcount
