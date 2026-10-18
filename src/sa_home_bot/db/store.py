from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from math import sqrt
from typing import Any

from sa_home_bot.db.connection import Database
from sa_home_bot.db.state_tables import (
    HEALTH_STATES,
    HOST_STATES,
    StateTable,
    iso_or_none,
    parse_iso_or_none,
)
from sa_home_bot.domain.host import (
    HostHealthDiff,
    HostMetricReading,
    HostMetricState,
)
from sa_home_bot.domain.models import (
    DiskSummary,
    HealthDiff,
    HealthState,
    KnownState,
    SensorReading,
    SmartSnapshot,
)
from sa_home_bot.domain.policy import BaselineStats

NOTIF_ALERT = "alert"
NOTIF_CLEARED = "cleared"

ACTION_TICKS_PREFIX = "action_ticks:"

DISK_SUMMARIES_KEY = "disk_summaries_cache"


def _item(row: Any) -> dict:
    item = dict(row)
    try:
        traits = json.loads(item.get("traits") or "[]")
    except json.JSONDecodeError:
        traits = []
    item["traits"] = [t for t in traits if isinstance(t, str)] if isinstance(traits, list) else []
    return item


OUTBOX_KIND_ALERT = "alert"
OUTBOX_KIND_TASK = "task"
OUTBOX_KIND_DELIVER = "deliver_message"
OUTBOX_KIND_INTERACTIVE = "interactive"
OUTBOX_TTL: dict[str, timedelta] = {
    OUTBOX_KIND_ALERT: timedelta(hours=6),
    OUTBOX_KIND_TASK: timedelta(hours=24),
    OUTBOX_KIND_DELIVER: timedelta(hours=24),
    OUTBOX_KIND_INTERACTIVE: timedelta(minutes=15),
}


def outbox_ttl(kind: str) -> timedelta:
    return OUTBOX_TTL.get(kind, OUTBOX_TTL[OUTBOX_KIND_INTERACTIVE])


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _pending_action_row(row) -> dict:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return data


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        cur = await self.db.conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = await self.db.conn.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


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

    async def state_keys(self, prefix: str) -> list[str]:
        cur = await self.db.conn.execute(
            "SELECT key FROM app_state WHERE substr(key, 1, ?) = ? ORDER BY key",
            (len(prefix), prefix),
        )
        return [row["key"] for row in await cur.fetchall()]

    async def delete_state(self, key: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM app_state WHERE key=?", (key,))

    async def get_action_ticks(self, action_key: str) -> list[datetime]:
        raw = await self.get_state(ACTION_TICKS_PREFIX + action_key)
        if not raw:
            return []
        try:
            return [datetime.fromisoformat(s) for s in json.loads(raw)]
        except (ValueError, TypeError):
            return []

    async def set_action_ticks(self, action_key: str, ticks: list[datetime]) -> None:
        await self.set_state(
            ACTION_TICKS_PREFIX + action_key, json.dumps([iso_or_none(t) for t in ticks])
        )


    async def outbox_enqueue(
        self,
        chat_id: int,
        kind: str,
        payload: dict,
        *,
        now: datetime,
        dedup_key: str | None = None,
        message_thread_id: int | None = None,
    ) -> int:
        async with self.db.transaction() as conn:
            if dedup_key is not None:
                await conn.execute(
                    "DELETE FROM outbox WHERE chat_id=? AND dedup_key=?", (chat_id, dedup_key)
                )
            cur = await conn.execute(
                "INSERT INTO outbox(chat_id, kind, payload_json, created_at, dedup_key, "
                "expires_at, message_thread_id) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    _utc_iso(now),
                    dedup_key,
                    _utc_iso(now + outbox_ttl(kind)),
                    message_thread_id,
                ),
            )
            return cur.lastrowid

    async def outbox_due(self, now: datetime, limit: int = 100) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT * FROM outbox WHERE expires_at > ? ORDER BY created_at, id LIMIT ?",
            (_utc_iso(now), limit),
        )
        rows = []
        for row in await cur.fetchall():
            data = dict(row)
            data["payload"] = json.loads(data.pop("payload_json") or "{}")
            data["created_at"] = datetime.fromisoformat(data["created_at"])
            rows.append(data)
        return rows

    async def outbox_delete(self, outbox_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM outbox WHERE id=?", (outbox_id,))

    async def outbox_bump_attempts(self, outbox_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE outbox SET attempts=attempts+1 WHERE id=?", (outbox_id,))

    async def outbox_expire(self, now: datetime) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM outbox WHERE expires_at <= ?", (_utc_iso(now),)
            )
            return cur.rowcount


    async def start_job_run(self, job_type: str, started_at: datetime) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO job_runs(job_type, started_at, status) VALUES(?, ?, 'running')",
                (job_type, iso_or_none(started_at)),
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
                (status, iso_or_none(finished_at), error, metrics_json, run_id),
            )

    async def recent_job_runs(self, limit: int = 10) -> list[dict]:
        return await self._fetch_all("SELECT * FROM job_runs ORDER BY id DESC LIMIT ?", (limit,))

    async def job_run_counts(self) -> dict[str, int]:
        cur = await self.db.conn.execute(
            "SELECT status, COUNT(*) AS n FROM job_runs GROUP BY status"
        )
        rows = await cur.fetchall()
        return {r["status"]: r["n"] for r in rows}


    async def get_known_states(self) -> dict[str, KnownState]:
        return await self._known_states("health_states")

    async def get_known_host_states(self) -> dict[str, KnownState]:
        return await self._known_states("host_metric_states")

    async def apply_diff(self, diff: HealthDiff, now: datetime) -> None:
        async with self.db.transaction() as conn:
            await HEALTH_STATES.apply(conn, diff.states, diff.transitions, now)

    async def apply_host_diff(self, diff: HostHealthDiff, now: datetime) -> None:
        async with self.db.transaction() as conn:
            await HOST_STATES.apply(conn, diff.states, diff.transitions, now)

    async def pending_alerts(self) -> list[HealthState]:
        return await self._pending_alerts(HEALTH_STATES)

    async def pending_clears(self) -> list[HealthState]:
        return await self._pending_clears(HEALTH_STATES)

    async def pending_host_alerts(self) -> list[HostMetricState]:
        return await self._pending_alerts(HOST_STATES)

    async def pending_host_clears(self) -> list[HostMetricState]:
        return await self._pending_clears(HOST_STATES)

    async def mark_alert_notified(self, component_id: str, at: datetime) -> None:
        await self._mark_notified(HEALTH_STATES, "notified_alert_at", component_id, at)

    async def mark_cleared_notified(self, component_id: str, at: datetime) -> None:
        await self._mark_notified(HEALTH_STATES, "notified_cleared_at", component_id, at)

    async def mark_host_alert_notified(self, component_id: str, at: datetime) -> None:
        await self._mark_notified(HOST_STATES, "notified_alert_at", component_id, at)

    async def mark_host_cleared_notified(self, component_id: str, at: datetime) -> None:
        await self._mark_notified(HOST_STATES, "notified_cleared_at", component_id, at)

    async def _known_states(self, table: str) -> dict[str, KnownState]:
        rows = await self._fetch_all(
            f"SELECT component_id, status, consecutive_count, alerting_since FROM {table}"
        )
        return {
            r["component_id"]: KnownState(
                component_id=r["component_id"],
                status=r["status"],
                consecutive_count=r["consecutive_count"],
                alerting_since=parse_iso_or_none(r["alerting_since"]),
            )
            for r in rows
        }

    async def _pending_alerts(self, table: StateTable) -> list:
        cur = await self.db.conn.execute(
            f"SELECT * FROM {table.table} WHERE status='alerting' AND notified_alert_at IS NULL"
        )
        return [table.row_to_state(r) for r in await cur.fetchall()]

    async def _pending_clears(self, table: StateTable) -> list:
        cur = await self.db.conn.execute(
            f"SELECT * FROM {table.table} WHERE status='ok' "
            "AND notified_alert_at IS NOT NULL AND notified_cleared_at IS NULL"
        )
        return [table.row_to_state(r) for r in await cur.fetchall()]

    async def _mark_notified(
        self, table: StateTable, column: str, component_id: str, at: datetime
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                f"UPDATE {table.table} SET {column}=? WHERE component_id=?",
                (iso_or_none(at), component_id),
            )

    async def record_readings(self, readings: list[SensorReading]) -> None:
        await self._insert_readings(
            "readings(component_id, temperature_c, taken_at)",
            [(r.component_id, r.temperature_c, iso_or_none(r.taken_at)) for r in readings],
        )

    async def record_host_readings(self, readings: list[HostMetricReading]) -> None:
        await self._insert_readings(
            "host_readings(component_id, metric, value, taken_at)",
            [(r.component_id, r.metric, r.value, iso_or_none(r.taken_at)) for r in readings],
        )

    async def prune_readings(self, keep_per_component: int) -> int:
        return await self._prune_keep_last("readings", keep_per_component)

    async def prune_host_readings(self, keep_per_component: int) -> int:
        return await self._prune_keep_last("host_readings", keep_per_component)

    async def _insert_readings(self, target: str, rows: list[tuple]) -> None:
        if not rows:
            return
        marks = ", ".join("?" for _ in rows[0])
        async with self.db.transaction() as conn:
            await conn.executemany(f"INSERT INTO {target} VALUES({marks})", rows)

    async def _prune_keep_last(self, table: str, keep_per_component: int) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                f"DELETE FROM {table} WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY component_id ORDER BY id DESC"
                f"    ) AS rn FROM {table}"
                "  ) WHERE rn > ?"
                ")",
                (keep_per_component,),
            )
            return cur.rowcount

    async def get_all_states(self) -> list[HealthState]:
        rows = await self._fetch_all("SELECT * FROM health_states ORDER BY kind, component_id")
        return [HEALTH_STATES.row_to_state(r) for r in rows]


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
                (component_id, chat_id, kind, message_id, iso_or_none(sent_at)),
            )

    async def get_alert_message_id(self, component_id: str, chat_id: int) -> int | None:
        cur = await self.db.conn.execute(
            "SELECT message_id FROM health_notifications "
            "WHERE component_id=? AND chat_id=? AND kind=?",
            (component_id, chat_id, NOTIF_ALERT),
        )
        row = await cur.fetchone()
        return row["message_id"] if row else None


    async def get_all_host_states(self) -> list[HostMetricState]:
        rows = await self._fetch_all("SELECT * FROM host_metric_states ORDER BY component_id")
        return [HOST_STATES.row_to_state(r) for r in rows]

    async def host_trend(self, metric: str, hours: int, now: datetime) -> list[dict]:
        cutoff = (now - timedelta(hours=int(hours))).isoformat()
        cur = await self.db.conn.execute(
            "SELECT substr(taken_at, 1, 13) || ':' || "
            "  printf('%02d', (CAST(substr(taken_at, 15, 2) AS INTEGER) / 10) * 10) AS bucket, "
            "  AVG(value) AS avg_value, MAX(value) AS max_value, COUNT(*) AS n "
            "FROM host_readings "
            "WHERE component_id = ? AND taken_at >= ? "
            "GROUP BY bucket ORDER BY bucket",
            (f"host:{metric}", cutoff),
        )
        return [
            {
                "bucket": r["bucket"],
                "avg": round(r["avg_value"], 2),
                "max": round(r["max_value"], 2),
                "samples": r["n"],
            }
            for r in await cur.fetchall()
        ]


    async def baseline_stats(self, component_id: str, window: int) -> BaselineStats:
        cur = await self.db.conn.execute(
            "SELECT COUNT(*) AS n, AVG(t) AS m, AVG(t*t) AS mm FROM ("
            "  SELECT temperature_c AS t FROM readings WHERE component_id=? "
            "  ORDER BY id DESC LIMIT ?"
            ")",
            (component_id, window),
        )
        row = await cur.fetchone()
        n = row["n"] or 0
        if n == 0:
            return BaselineStats(count=0, mean=0.0, std=0.0)
        mean = row["m"]
        variance = max(0.0, (row["mm"] or 0.0) - mean * mean)
        return BaselineStats(count=n, mean=mean, std=sqrt(variance))


    async def get_smart_snapshot(self, component_id: str) -> SmartSnapshot | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM smart_snapshots WHERE component_id=?", (component_id,)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return SmartSnapshot(
            component_id=row["component_id"],
            label=row["label"],
            health=row["health"],
            attrs={int(k): int(v) for k, v in json.loads(row["attrs_json"]).items()},
            taken_at=datetime.fromisoformat(row["taken_at"]),
        )

    async def get_smart_health_map(self) -> dict[str, str | None]:
        cur = await self.db.conn.execute("SELECT component_id, health FROM smart_snapshots")
        rows = await cur.fetchall()
        return {r["component_id"].removeprefix("disk:"): r["health"] for r in rows}

    async def save_smart_snapshot(self, snap: SmartSnapshot) -> None:
        attrs_json = json.dumps({str(k): v for k, v in snap.attrs.items()})
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO smart_snapshots(component_id, label, health, attrs_json, taken_at) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(component_id) DO UPDATE SET "
                "label=excluded.label, health=excluded.health, "
                "attrs_json=excluded.attrs_json, taken_at=excluded.taken_at",
                (
                    snap.component_id,
                    snap.label,
                    snap.health,
                    attrs_json,
                    iso_or_none(snap.taken_at),
                ),
            )

    async def save_disk_summaries(self, disks: list[DiskSummary]) -> None:
        await self.set_state(DISK_SUMMARIES_KEY, json.dumps([asdict(d) for d in disks]))

    async def get_disk_summaries(self) -> list[DiskSummary] | None:
        raw = await self.get_state(DISK_SUMMARIES_KEY)
        if raw is None:
            return None
        return [DiskSummary(**d) for d in json.loads(raw)]


    async def record_ai_turn(
        self,
        chat_id: int,
        message_id: int,
        dialogue_id: int,
        role: str,
        content: str,
        at: datetime,
        user_id: int | None = None,
        user_name: str | None = None,
        photo_path: str | None = None,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO ai_turns(chat_id, message_id, dialogue_id, role, content, "
                "created_at, user_id, user_name, photo_path) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    message_id,
                    dialogue_id,
                    role,
                    content,
                    iso_or_none(at),
                    user_id,
                    user_name,
                    photo_path,
                ),
            )

    async def ai_turn(self, chat_id: int, message_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM ai_turns WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )

    async def ai_turns_for_dialogue(self, chat_id: int, dialogue_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM ai_turns WHERE chat_id=? AND dialogue_id=? ORDER BY message_id",
            (chat_id, dialogue_id),
        )

    async def set_ai_turn_content(self, chat_id: int, message_id: int, content: str) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE ai_turns SET content=? WHERE chat_id=? AND message_id=?",
                (content, chat_id, message_id),
            )
            await conn.execute(
                "UPDATE ai_turns_fts SET content=? WHERE chat_id=? AND message_id=?",
                (content.replace("ё", "е").replace("Ё", "Е"), chat_id, message_id),
            )


    async def update_state(self, key: str, fn: Callable[[str | None], str | None]) -> str | None:
        for _ in range(8):
            old = await self.get_state(key)
            new = fn(old)
            if new == old:
                return new
            async with self.db.transaction() as conn:
                if old is None:
                    cur = await conn.execute(
                        "INSERT INTO app_state(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO NOTHING",
                        (key, new),
                    )
                elif new is None:
                    cur = await conn.execute(
                        "DELETE FROM app_state WHERE key=? AND value=?", (key, old)
                    )
                else:
                    cur = await conn.execute(
                        "UPDATE app_state SET value=? WHERE key=? AND value=?", (new, key, old)
                    )
                changed = cur.rowcount == 1
            if changed:
                return new
        raise RuntimeError(f"app_state[{key}]: не удалось записать — слишком частые правки")

    async def add_away_media(
        self,
        chat_id: int,
        message_id: int,
        kind: str,
        path: str | None,
        file_id: str | None,
        duration_s: int,
        at: datetime,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO away_media(chat_id, message_id, kind, path, file_id, "
                "duration_s, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, kind, path, file_id, duration_s, iso_or_none(at)),
            )

    async def away_media_for_chat(self, chat_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM away_media WHERE chat_id=? ORDER BY message_id",
            (chat_id,),
        )

    async def delete_away_media(self, chat_id: int, message_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "DELETE FROM away_media WHERE chat_id=? AND message_id=?", (chat_id, message_id)
            )

    async def latest_photo_turn(self, chat_id: int, dialogue_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM ai_turns WHERE chat_id=? AND dialogue_id=? AND photo_path IS NOT NULL "
            "ORDER BY message_id DESC LIMIT 1",
            (chat_id, dialogue_id),
        )

    async def chat_participants(self, chat_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT user_id, user_name, MIN(message_id) AS first_message_id "
            "FROM ai_turns WHERE chat_id=? AND role='user' AND user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY first_message_id",
            (chat_id,),
        )

    async def record_tool_call(
        self,
        chat_id: int,
        dialogue_id: int,
        trigger_message_id: int | None,
        tool_name: str,
        args: dict[str, Any],
        result: str,
        at: datetime,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO ai_tool_calls(chat_id, dialogue_id, trigger_message_id, "
                "tool_name, args_json, result, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    dialogue_id,
                    trigger_message_id,
                    tool_name,
                    json.dumps(args, ensure_ascii=False),
                    result,
                    iso_or_none(at),
                ),
            )

    async def tool_calls_for_dialogue(self, chat_id: int, dialogue_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM ai_tool_calls WHERE chat_id=? AND dialogue_id=? ORDER BY id",
            (chat_id, dialogue_id),
        )

    async def latest_ai_dialogue(self, chat_id: int) -> int | None:
        cur = await self.db.conn.execute(
            "SELECT dialogue_id FROM ai_turns WHERE chat_id=? ORDER BY message_id DESC LIMIT 1",
            (chat_id,),
        )
        row = await cur.fetchone()
        return row["dialogue_id"] if row else None


    async def latest_dialogue_summary(self, chat_id: int, dialogue_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM ai_dialogue_summaries WHERE chat_id=? AND dialogue_id=? "
            "ORDER BY upto_message_id DESC, id DESC LIMIT 1",
            (chat_id, dialogue_id),
        )

    async def add_dialogue_summary(
        self, chat_id: int, dialogue_id: int, upto_message_id: int, summary: str, at: datetime
    ) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO ai_dialogue_summaries(chat_id, dialogue_id, upto_message_id, "
                "summary, created_at) VALUES(?, ?, ?, ?, ?)",
                (chat_id, dialogue_id, upto_message_id, summary, iso_or_none(at)),
            )
            return cur.lastrowid

    async def dialogue_context_state(self, chat_id: int, dialogue_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM ai_dialogue_context WHERE chat_id=? AND dialogue_id=?",
            (chat_id, dialogue_id),
        )

    async def save_dialogue_context_state(
        self,
        chat_id: int,
        dialogue_id: int,
        *,
        prompt_tokens: int,
        sent_chars: int,
        num_ctx: int | None,
        done_reason: str | None,
        at: datetime,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO ai_dialogue_context(chat_id, dialogue_id, prompt_tokens, "
                "sent_chars, num_ctx, done_reason, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id, dialogue_id) DO UPDATE SET "
                "prompt_tokens=excluded.prompt_tokens, sent_chars=excluded.sent_chars, "
                "num_ctx=excluded.num_ctx, done_reason=excluded.done_reason, "
                "updated_at=excluded.updated_at",
                (
                    chat_id,
                    dialogue_id,
                    prompt_tokens,
                    sent_chars,
                    num_ctx,
                    done_reason,
                    iso_or_none(at),
                ),
            )

    async def latest_known_num_ctx(self) -> int | None:
        cur = await self.db.conn.execute(
            "SELECT num_ctx FROM ai_dialogue_context WHERE num_ctx IS NOT NULL "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return int(row["num_ctx"]) if row else None

    async def search_dialogue_turns(
        self, chat_id: int, dialogue_id: int, match: str, *, max_message_id: int, limit: int
    ) -> list[dict]:
        return await self._fetch_all(
            "SELECT CAST(message_id AS INTEGER) AS message_id, bm25(ai_turns_fts) AS score "
            "FROM ai_turns_fts WHERE ai_turns_fts MATCH ? AND chat_id=? AND dialogue_id=? "
            "AND message_id<=? ORDER BY score LIMIT ?",
            (match, chat_id, dialogue_id, max_message_id, limit),
        )


    async def add_person_claims(self, claims: list[dict[str, Any]]) -> None:
        async with self.db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO person_claims(subject_id, field, value, by_id, strength, at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (c["subject_id"], c["field"], c["value"], c["by_id"], c["strength"], c["at"])
                    for c in claims
                ],
            )

    async def person_claims(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM person_claims ORDER BY id")

    async def upsert_person_profile(
        self, user_id: int, first_name: str, last_name: str, username: str, now: datetime
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO person_profiles(user_id, first_name, last_name, username, seen_at) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
                "first_name=excluded.first_name, last_name=excluded.last_name, "
                "username=excluded.username, seen_at=excluded.seen_at",
                (user_id, first_name, last_name, username, iso_or_none(now)),
            )

    async def person_profiles(self) -> list[dict]:
        return await self._fetch_all("SELECT * FROM person_profiles")

    async def latest_turn_user_names(self) -> list[dict]:
        return await self._fetch_all(
            "SELECT user_id, user_name, MAX(created_at) AS at FROM ai_turns "
            "WHERE user_id IS NOT NULL AND user_name IS NOT NULL GROUP BY user_id"
        )


    async def get_relationship(self, relationship_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM guest_relationships WHERE id=?",
            (relationship_id,),
        )

    async def relationships_for(self, chat_id: int, status: str = "confirmed") -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM guest_relationships WHERE status=? AND (guest_a=? OR guest_b=?) "
            "ORDER BY id",
            (status, chat_id, chat_id),
        )

    async def add_confirmed_relationship(
        self, guest_a: int, guest_b: int, relation: str, created_at: datetime, now: datetime
    ) -> dict:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO guest_relationships(guest_a, guest_b, relation, status, "
                "proposed_by, created_at, confirmed_at) VALUES(?, ?, ?, 'confirmed', ?, ?, ?)",
                (guest_a, guest_b, relation, guest_a, iso_or_none(created_at), iso_or_none(now)),
            )
            row_id = cur.lastrowid
        row = await self.get_relationship(row_id)
        assert row is not None
        return row


    async def create_pending_action(
        self,
        kind: str,
        initiator: int,
        addressee: int | None,
        payload: dict,
        expires_at: datetime,
        now: datetime,
    ) -> dict:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO pending_actions(kind, initiator, addressee, payload_json, status, "
                "expires_at, created_at) VALUES(?, ?, ?, ?, 'draft', ?, ?)",
                (
                    kind,
                    initiator,
                    addressee,
                    json.dumps(payload, ensure_ascii=False),
                    iso_or_none(expires_at),
                    iso_or_none(now),
                ),
            )
            row_id = cur.lastrowid
        row = await self.get_pending_action(row_id)
        assert row is not None
        return row

    async def get_pending_action(self, action_id: int) -> dict | None:
        cur = await self.db.conn.execute("SELECT * FROM pending_actions WHERE id=?", (action_id,))
        row = await cur.fetchone()
        return _pending_action_row(row) if row else None

    async def transition_pending_action(
        self,
        action_id: int,
        from_statuses: tuple[str, ...],
        to_status: str,
        now: datetime,
        *,
        expires_at: datetime | None = None,
        decided_by: int | None = None,
        reason: str | None = None,
    ) -> dict | None:
        placeholders = ", ".join("?" for _ in from_statuses)
        sets = ["status=?"]
        params: list[Any] = [to_status]
        if to_status == "pending":
            sets += ["submitted_at=?", "expires_at=?"]
            params += [iso_or_none(now), iso_or_none(expires_at)]
        else:
            sets += ["decided_at=?", "decided_by=?", "reason=?"]
            params += [iso_or_none(now), decided_by, reason]
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                f"UPDATE pending_actions SET {', '.join(sets)} "
                f"WHERE id=? AND status IN ({placeholders})",
                (*params, action_id, *from_statuses),
            )
            changed = cur.rowcount
        if changed != 1:
            return None
        return await self.get_pending_action(action_id)

    async def set_pending_action_message(
        self, action_id: int, column: str, message_id: int
    ) -> None:
        if column not in (
            "draft_message_id",
            "offer_message_id",
            "notice_message_id",
            "welcome_message_id",
        ):
            raise ValueError(f"не колонка сообщения формы: {column}")
        async with self.db.transaction() as conn:
            await conn.execute(
                f"UPDATE pending_actions SET {column}=? WHERE id=?", (message_id, action_id)
            )

    async def open_pending_actions(
        self, kind: str | None = None, chat_id: int | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM pending_actions WHERE status IN ('draft', 'pending')"
        params: list[Any] = []
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind)
        if chat_id is not None:
            sql += " AND (initiator=? OR (status='pending' AND addressee=?))"
            params += [chat_id, chat_id]
        cur = await self.db.conn.execute(sql + " ORDER BY id", params)
        return [_pending_action_row(r) for r in await cur.fetchall()]

    async def pending_actions_needing_delivery(self) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT * FROM pending_actions WHERE "
            "(status='pending' AND offer_message_id IS NULL) OR "
            "(status IN ('accepted', 'rejected', 'expired', 'cancelled') "
            " AND notice_message_id IS NULL "
            " AND NOT (status='cancelled' AND decided_by=initiator)) OR "
            "(status='accepted' AND welcome_message_id IS NULL) "
            "ORDER BY id"
        )
        return [_pending_action_row(r) for r in await cur.fetchall()]


    async def create_task(
        self,
        dst_node: str,
        dst_service: str,
        action: str,
        args: dict,
        timeout_s: float,
        meta: dict,
        due_at: datetime,
        created_at: datetime,
    ) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO tasks(dst_node, dst_service, action, args_json, timeout_s, "
                "meta_json, due_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dst_node,
                    dst_service,
                    action,
                    json.dumps(args, ensure_ascii=False),
                    timeout_s,
                    json.dumps(meta, ensure_ascii=False),
                    iso_or_none(due_at),
                    iso_or_none(created_at),
                ),
            )
            return cur.lastrowid

    async def due_tasks(self, now: datetime) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM tasks WHERE fired_at IS NULL AND due_at<=? ORDER BY due_at",
            (iso_or_none(now),),
        )

    async def get_pending_task(self, task_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM tasks WHERE id=? AND fired_at IS NULL",
            (task_id,),
        )

    async def mark_task_fired(self, task_id: int, at: datetime) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET fired_at=? WHERE id=?", (iso_or_none(at), task_id))

    async def tasks_needing_prewake(self, deadline: datetime) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM tasks WHERE fired_at IS NULL AND prewake_done=0 AND due_at<=?",
            (iso_or_none(deadline),),
        )

    async def mark_task_prewake_done(self, task_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET prewake_done=1 WHERE id=?", (task_id,))


    async def create_invite(
        self,
        code: str,
        issued_by_chat_id: int,
        issued_by_user_id: int | None,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO invites(code, issued_by_chat_id, issued_by_user_id, "
                "created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                (
                    code,
                    issued_by_chat_id,
                    issued_by_user_id,
                    iso_or_none(created_at),
                    iso_or_none(expires_at),
                ),
            )

    async def redeem_invite(
        self,
        code: str,
        *,
        at: datetime,
        chat_id: int,
        user_id: int | None,
        user_name: str | None,
    ) -> dict | None:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE invites SET redeemed_at=?, redeemed_chat_id=?, redeemed_user_id=?, "
                "redeemed_user_name=? WHERE code=? AND redeemed_at IS NULL "
                "AND revoked_at IS NULL AND expires_at>?",
                (iso_or_none(at), chat_id, user_id, user_name, code, iso_or_none(at)),
            )
            if cur.rowcount == 0:
                return None
            cur = await conn.execute("SELECT * FROM invites WHERE code=?", (code,))
            row = await cur.fetchone()
            return dict(row) if row else None

    async def find_invite(self, code: str) -> dict | None:
        return await self._fetch_one("SELECT * FROM invites WHERE code=?", (code,))

    async def open_invites(self, now: datetime) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM invites WHERE redeemed_at IS NULL AND revoked_at IS NULL "
            "AND expires_at>? ORDER BY created_at",
            (iso_or_none(now),),
        )

    async def revoke_invite(self, code: str, at: datetime) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE invites SET revoked_at=? WHERE code=? AND redeemed_at IS NULL "
                "AND revoked_at IS NULL",
                (iso_or_none(at), code),
            )
            return cur.rowcount > 0


    async def record_event(
        self, event_type: str, node: str | None, text: str, at: datetime
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO swarm_events(event_type, node, text, created_at) VALUES(?, ?, ?, ?)",
                (event_type, node, text, iso_or_none(at)),
            )

    async def recent_events(
        self,
        *,
        node: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list[object] = []
        if node is not None:
            clauses.append("node = ?")
            params.append(node)
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(iso_or_none(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return await self._fetch_all(
            f"SELECT event_type, node, text, created_at FROM swarm_events {where} "
            "ORDER BY id DESC LIMIT ?",
            params,
        )

    async def prune_swarm_events(self, keep_last: int = 500) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM swarm_events WHERE id NOT IN "
                "(SELECT id FROM swarm_events ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
            return cur.rowcount


    async def add_event_waiter(
        self, task_id: int, node: str, event_type: str, created_at: datetime
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO event_waiters(task_id, node, event_type, created_at) "
                "VALUES(?, ?, ?, ?)",
                (task_id, node, event_type, iso_or_none(created_at)),
            )

    async def pop_event_waiter_for(self, node: str, event_type: str) -> int | None:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "SELECT task_id FROM event_waiters WHERE node=? AND event_type=? "
                "ORDER BY task_id LIMIT 1",
                (node, event_type),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            task_id = row["task_id"]
            await conn.execute("DELETE FROM event_waiters WHERE task_id=?", (task_id,))
            return task_id

    async def pop_event_waiter(self, task_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("DELETE FROM event_waiters WHERE task_id=?", (task_id,))


    async def add_image(
        self,
        *,
        chat_id: int,
        author: str | None,
        prompt_ru: str,
        prompt_en: str,
        caption: str,
        width: int,
        height: int,
        colors: int,
        png: bytes,
        now: datetime,
        purpose: str = "chat",
        params: str | None = None,
    ) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO images (chat_id, author, prompt_ru, prompt_en, caption, "
                "width, height, colors, png, created_at, purpose, params) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (chat_id, author, prompt_ru, prompt_en, caption, width, height, colors,
                 png, iso_or_none(now), purpose, params),
            )
            image_id = cur.lastrowid
            await conn.execute(
                "INSERT INTO images_fts (rowid, caption, prompt_ru, prompt_en) VALUES (?,?,?,?)",
                (image_id, caption, prompt_ru, prompt_en),
            )
            return int(image_id)

    async def set_image_sent(
        self, image_id: int, telegram_file_id: str | None, message_id: int | None
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE images SET telegram_file_id=?, message_id=COALESCE(?, message_id) "
                "WHERE id=?",
                (telegram_file_id, message_id, image_id),
            )

    async def get_image(self, chat_id: int, image_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM images WHERE id=? AND chat_id=? AND purpose='chat'",
            (image_id, chat_id),
        )

    async def search_images(self, chat_id: int, query: str, limit: int = 5) -> list[dict]:
        terms = []
        for word in re.findall(r"\w+", query.lower()):
            if len(word) < 3:
                continue
            stem = word[: max(3, len(word) - 2)] if len(word) > 4 else word
            terms.append(f'"{stem}"*')
        if not terms:
            return []
        return await self._fetch_all(
            "SELECT i.id, i.caption, i.prompt_ru, i.created_at, i.telegram_file_id "
            "FROM images_fts f JOIN images i ON i.id = f.rowid "
            "WHERE images_fts MATCH ? AND i.chat_id=? AND i.purpose='chat' "
            "ORDER BY bm25(images_fts) LIMIT ?",
            (" OR ".join(terms), chat_id, limit),
        )

    async def recent_images(self, chat_id: int, limit: int = 5) -> list[dict]:
        return await self._fetch_all(
            "SELECT id, caption, prompt_ru, created_at, telegram_file_id FROM images "
            "WHERE chat_id=? AND purpose='chat' ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )

    async def count_images_since(
        self, chat_id: int, since: datetime, purpose: str = "chat"
    ) -> int:
        cur = await self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM images WHERE chat_id=? AND created_at>=? "
            "AND purpose=?",
            (chat_id, iso_or_none(since), purpose),
        )
        row = await cur.fetchone()
        return int(row["n"])


    async def image_by_id(self, image_id: int) -> dict | None:
        return await self._fetch_one("SELECT * FROM images WHERE id=?", (image_id,))

    ALFRED_IMAGE_PURPOSES = ("chat", "photo", "item")

    async def alfred_image(
        self, chat_id: int, *, image_id: int | None = None, message_id: int | None = None
    ) -> dict | None:
        marks = ",".join("?" * len(self.ALFRED_IMAGE_PURPOSES))
        sql = f"SELECT * FROM images WHERE chat_id=? AND purpose IN ({marks})"
        params: list[object] = [chat_id, *self.ALFRED_IMAGE_PURPOSES]
        if image_id is not None:
            sql += " AND id=?"
            params.append(image_id)
        elif message_id is not None:
            sql += " AND message_id=?"
            params.append(message_id)
        else:
            sql += " AND message_id IS NOT NULL"
        return await self._fetch_one(sql + " ORDER BY id DESC LIMIT 1", params)

    async def image_by_message(self, chat_id: int, message_id: int) -> dict | None:
        return await self._fetch_one(
            "SELECT * FROM images WHERE chat_id=? AND message_id=?",
            (chat_id, message_id),
        )

    async def set_image_purpose(self, image_id: int, purpose: str) -> bool:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE images SET purpose=? WHERE id=?", (purpose, image_id)
            )
            return cur.rowcount > 0

    async def count_images_by_purpose(self, purpose: str) -> int:
        cur = await self.db.conn.execute(
            "SELECT COUNT(*) AS n FROM images WHERE purpose=?", (purpose,)
        )
        row = await cur.fetchone()
        return int(row["n"])

    async def delete_images_by_purpose(self, purpose: str) -> int:
        async with self.db.transaction() as conn:
            await conn.execute(
                "DELETE FROM images_fts WHERE rowid IN (SELECT id FROM images WHERE purpose=?)",
                (purpose,),
            )
            cur = await conn.execute("DELETE FROM images WHERE purpose=?", (purpose,))
            return cur.rowcount


    async def add_item(
        self,
        *,
        type: str,
        owner_user_id: int,
        traits: list[str],
        image_id: int | None,
        origin: str,
        now: datetime,
        note: str | None = None,
    ) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "INSERT INTO items (type, owner_user_id, traits, image_id, origin, note, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (type, owner_user_id, json.dumps(traits, ensure_ascii=False), image_id, origin,
                 note, iso_or_none(now)),
            )
            item_id = int(cur.lastrowid)
            await conn.execute(
                "INSERT INTO item_events (item_id, kind, user_id, data, created_at) "
                "VALUES (?,?,?,?,?)",
                (
                    item_id,
                    "created",
                    owner_user_id,
                    json.dumps({"origin": origin}),
                    iso_or_none(now),
                ),
            )
            return item_id

    async def item_by_id(self, item_id: int) -> dict | None:
        cur = await self.db.conn.execute("SELECT * FROM items WHERE id=?", (item_id,))
        row = await cur.fetchone()
        return _item(row) if row else None

    async def items_of(self, owner_user_id: int, type: str | None = None) -> list[dict]:
        sql = "SELECT * FROM items WHERE owner_user_id=? AND status!='consumed'"
        args: tuple = (owner_user_id,)
        if type is not None:
            sql += " AND type=?"
            args += (type,)
        cur = await self.db.conn.execute(sql + " ORDER BY id", args)
        return [_item(r) for r in await cur.fetchall()]

    async def set_item_image(self, item_id: int, image_id: int, traits: list[str]) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE items SET image_id=?, traits=? WHERE id=?",
                (image_id, json.dumps(traits, ensure_ascii=False), item_id),
            )

    async def consume_item(self, item_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE items SET status='consumed' WHERE id=?", (item_id,))

    async def update_item(
        self, item_id: int, *, traits: list[str] | None = None, note: str | None = None
    ) -> None:
        async with self.db.transaction() as conn:
            if traits is not None:
                await conn.execute(
                    "UPDATE items SET traits=? WHERE id=?",
                    (json.dumps(traits, ensure_ascii=False), item_id),
                )
            if note is not None:
                await conn.execute("UPDATE items SET note=? WHERE id=?", (note, item_id))

    async def add_item_event(
        self,
        item_id: int,
        kind: str,
        *,
        user_id: int | None,
        chat_id: int | None,
        data: dict | None,
        now: datetime,
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO item_events (item_id, kind, user_id, chat_id, data, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (item_id, kind, user_id, chat_id,
                 json.dumps(data, ensure_ascii=False) if data is not None else None,
                 iso_or_none(now)),
            )

    async def item_events(self, item_id: int) -> list[dict]:
        return await self._fetch_all(
            "SELECT * FROM item_events WHERE item_id=? ORDER BY id",
            (item_id,),
        )


    async def prune_job_runs(self, keep_last: int = 500) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM job_runs WHERE id NOT IN "
                "(SELECT id FROM job_runs ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
            return cur.rowcount
