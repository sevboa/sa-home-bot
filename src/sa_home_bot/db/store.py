"""Store — единственный слой доступа к БД. Голый SQL, без ORM.

Идемпотентность уведомлений: флаги `notified_alert_at` / `notified_cleared_at`
выставляются только после успешной отправки. Жизненный цикл одного цикла
перегрева: переход в alerting сбрасывает оба флага; отправка "перегрев"
выставляет `notified_alert_at`; переход в ok сбрасывает `notified_cleared_at`;
отправка "остыл" выставляет его.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from math import sqrt

from sa_home_bot.db.connection import Database
from sa_home_bot.domain.models import (
    ALERTING,
    OK,
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

# Префикс app_state-ключей: метки времени принятых ручных действий (лимитер).
ACTION_TICKS_PREFIX = "action_ticks:"

# app_state-ключ кэша DiskSummary (см. docstring класса) — пишет SensorScanJob
# раз в scan_cron, читает MonitorService.get_state() вместо живого опроса.
DISK_SUMMARIES_KEY = "disk_summaries_cache"


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

    async def get_action_ticks(self, action_key: str) -> list[datetime]:
        """Метки принятых ручных действий (для лимитера), ключ — «служба:действие»."""
        raw = await self.get_state(ACTION_TICKS_PREFIX + action_key)
        if not raw:
            return []
        try:
            return [datetime.fromisoformat(s) for s in json.loads(raw)]
        except (ValueError, TypeError):
            return []

    async def set_action_ticks(self, action_key: str, ticks: list[datetime]) -> None:
        await self.set_state(
            ACTION_TICKS_PREFIX + action_key, json.dumps([_iso(t) for t in ticks])
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

    # --- readings (история для BaselinePolicy) ---

    async def record_readings(self, readings: list[SensorReading]) -> None:
        """Записать срез показаний в историю (для baseline)."""
        if not readings:
            return
        async with self.db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO readings(component_id, temperature_c, taken_at) VALUES(?, ?, ?)",
                [(r.component_id, r.temperature_c, _iso(r.taken_at)) for r in readings],
            )

    async def baseline_stats(self, component_id: str, window: int) -> BaselineStats:
        """Статистика по последним ``window`` показаниям компонента."""
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

    async def prune_readings(self, keep_per_component: int) -> int:
        """Оставить последние ``keep_per_component`` показаний на каждый компонент."""
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM readings WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY component_id ORDER BY id DESC"
                "    ) AS rn FROM readings"
                "  ) WHERE rn > ?"
                ")",
                (keep_per_component,),
            )
            return cur.rowcount

    # --- smart_snapshots (baseline для дельты SMART) ---

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
        """realpath устройства -> последнее SMART-здоровье из снимков (для /status).

        Ключ — реальный путь (``/dev/sda``), т.е. component_id без префикса
        ``disk:``; сопоставляется с ``os.path.realpath`` физического диска.
        """
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
                (snap.component_id, snap.label, snap.health, attrs_json, _iso(snap.taken_at)),
            )

    async def save_disk_summaries(self, disks: list[DiskSummary]) -> None:
        await self.set_state(DISK_SUMMARIES_KEY, json.dumps([asdict(d) for d in disks]))

    async def get_disk_summaries(self) -> list[DiskSummary] | None:
        """Последний кэш DiskSummary или None — до первого прогона SensorScanJob."""
        raw = await self.get_state(DISK_SUMMARIES_KEY)
        if raw is None:
            return None
        return [DiskSummary(**d) for d in json.loads(raw)]

    # --- ai_turns (/ai, диалог с Альфредом) ---

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
    ) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO ai_turns(chat_id, message_id, dialogue_id, role, content, "
                "created_at, user_id, user_name) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, message_id, dialogue_id, role, content, _iso(at), user_id, user_name),
            )

    async def ai_turn(self, chat_id: int, message_id: int) -> dict | None:
        """Резолв reply: чьё это сообщение и к какому диалогу относится."""
        cur = await self.db.conn.execute(
            "SELECT * FROM ai_turns WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def ai_turns_for_dialogue(self, chat_id: int, dialogue_id: int) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT * FROM ai_turns WHERE chat_id=? AND dialogue_id=? ORDER BY message_id",
            (chat_id, dialogue_id),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def chat_participants(self, chat_id: int) -> list[dict]:
        """Различные пользователи (role='user'), кто когда-либо обращался к
        Альфреду в этом чате — для группового контекста промпта
        (bot/ai_flow.py::_build_context_note). Старые записи без user_id
        (до миграции) в выборку не попадают — это не критично, участники
        со временем "подтянутся" новыми обращениями."""
        cur = await self.db.conn.execute(
            "SELECT user_id, user_name, MIN(message_id) AS first_message_id "
            "FROM ai_turns WHERE chat_id=? AND role='user' AND user_id IS NOT NULL "
            "GROUP BY user_id ORDER BY first_message_id",
            (chat_id,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def latest_ai_dialogue(self, chat_id: int) -> int | None:
        """dialogue_id последнего хода в чате — для неявного продолжения
        разговора в личке (любое сообщение без /alfred и без reply)."""
        cur = await self.db.conn.execute(
            "SELECT dialogue_id FROM ai_turns WHERE chat_id=? ORDER BY message_id DESC LIMIT 1",
            (chat_id,),
        )
        row = await cur.fetchone()
        return row["dialogue_id"] if row else None

    # --- tasks (служба tasks, отложенные задачи роя, sa_home_bot/tasks/) ---

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
                    _iso(due_at),
                    _iso(created_at),
                ),
            )
            return cur.lastrowid

    async def due_tasks(self, now: datetime) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT * FROM tasks WHERE fired_at IS NULL AND due_at<=? ORDER BY due_at",
            (_iso(now),),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def mark_task_fired(self, task_id: int, at: datetime) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET fired_at=? WHERE id=?", (_iso(at), task_id))

    async def tasks_needing_prewake(self, deadline: datetime) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT * FROM tasks WHERE fired_at IS NULL AND prewake_done=0 AND due_at<=?",
            (_iso(deadline),),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def mark_task_prewake_done(self, task_id: int) -> None:
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE tasks SET prewake_done=1 WHERE id=?", (task_id,))

    # --- housekeeping ---

    async def prune_job_runs(self, keep_last: int = 500) -> int:
        async with self.db.transaction() as conn:
            cur = await conn.execute(
                "DELETE FROM job_runs WHERE id NOT IN "
                "(SELECT id FROM job_runs ORDER BY id DESC LIMIT ?)",
                (keep_last,),
            )
            return cur.rowcount
