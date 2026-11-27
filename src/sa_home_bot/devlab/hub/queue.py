from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from sa_home_bot.db.connection import Database
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import joblogs
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.utils.clock import utc_now

log = logging.getLogger(__name__)

READY_SQL = (
    "NOT EXISTS (SELECT 1 FROM json_each(jobs.params, '$.depends_on') d "
    "JOIN jobs dep ON dep.id = d.value WHERE dep.status != ?)"
)

QUEUE_ORDER_SQL = (
    "(kind = ?) DESC, "
    "CASE json_extract(params, '$.purpose') "
    "WHEN ? THEN 0 WHEN ? THEN 2 ELSE 1 END ASC, "
    "priority DESC, seq"
)
QUEUE_ORDER_ARGS = (protocol.JOB_KIND_TEST, protocol.PURPOSE_FIX, protocol.PURPOSE_BENCH)

PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
BRANCH_TAIL_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
JOB_ID_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
JOB_ID_LENGTH = 6
JOB_ID_ATTEMPTS = 20
DEFAULT_PAGE = 100

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    project TEXT NOT NULL REFERENCES projects(name),
    client TEXT NOT NULL,
    model TEXT NOT NULL,
    task TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    executor TEXT,
    created TEXT NOT NULL,
    started TEXT,
    finished TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    params TEXT NOT NULL DEFAULT '{}',
    active_seconds REAL NOT NULL DEFAULT 0,
    active_since TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (status, priority DESC, seq);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    direction TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created TEXT NOT NULL,
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS messages_job ON messages (job_id, direction, seq);
CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    counter INTEGER NOT NULL,
    changed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_live (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    data TEXT NOT NULL,
    changed TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    verdict TEXT NOT NULL,
    issues TEXT NOT NULL DEFAULT '[]',
    fix_commit TEXT,
    tests TEXT,
    client TEXT NOT NULL,
    created TEXT NOT NULL
);
"""

PARAMS_COLUMN = "params"
ADD_PARAMS_COLUMN = "ALTER TABLE jobs ADD COLUMN params TEXT NOT NULL DEFAULT '{}'"

ACTIVE_COLUMN = "active_seconds"
ADD_ACTIVE_COLUMNS = (
    "ALTER TABLE jobs ADD COLUMN active_seconds REAL NOT NULL DEFAULT 0",
    "ALTER TABLE jobs ADD COLUMN active_since TEXT",
)
OPEN_RUNNING_ACTIVE = "UPDATE jobs SET active_since = ? WHERE status = ?"
ACTIVE_FIELDS = ("active_seconds", "active_since")

JOB_COLUMNS = (
    "id, kind, project, client, model, task, base_branch, priority, status, executor, "
    "created, started, finished, tokens, cost_usd, params, active_seconds, active_since"
)


LIVE_KEYS = (
    protocol.FIELD_MODEL_STEPS,
    protocol.PAYLOAD_TOKENS,
    protocol.FIELD_SEGMENT_TOKENS,
    protocol.FIELD_REASONING_TOKENS,
    protocol.FIELD_USAGE_KNOWN,
    protocol.FIELD_PHASE,
    protocol.FIELD_CONTEXT_TOKENS,
    protocol.FIELD_CONTEXT_LIMIT,
    protocol.FIELD_FOLDS,
)


class TransitionRefused(HubError):
    def __init__(self, message: str, status: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    project: str
    client: str
    model: str
    task: str
    base_branch: str
    priority: int
    status: str
    executor: str | None
    created: str
    started: str | None
    finished: str | None
    tokens: int
    cost_usd: float
    params: dict[str, Any]
    active_seconds: float = 0.0
    active_since: str | None = None

    def active_s(self, now: datetime | None = None) -> int:
        if self.status == protocol.STATUS_RUNNING:
            return int(_closed(self, now or utc_now()))
        return int(self.active_seconds)


@dataclass(frozen=True)
class Message:
    seq: int
    job_id: str
    direction: str
    kind: str
    payload: dict[str, Any]
    created: str
    read: bool


def _job(row: aiosqlite.Row) -> Job:
    data = {key: row[key] for key in row.keys()}
    data["params"] = json.loads(data["params"])
    return Job(**data)


def wire_job(job: Job) -> dict[str, Any]:
    data = asdict(job)
    for name in ACTIVE_FIELDS:
        del data[name]
    return data


def _closed(job: Job, now: datetime) -> float:
    if not job.active_since:
        return job.active_seconds
    elapsed = (now - datetime.fromisoformat(job.active_since)).total_seconds()
    return job.active_seconds + max(0.0, elapsed)


def _message(row: aiosqlite.Row) -> Message:
    return Message(
        seq=row["seq"],
        job_id=row["job_id"],
        direction=row["direction"],
        kind=row["kind"],
        payload=json.loads(row["payload"]),
        created=row["created"],
        read=row["read_at"] is not None,
    )


def _spend(payload: dict[str, Any]) -> tuple[int, float]:
    tokens = payload.get(protocol.PAYLOAD_TOKENS, 0)
    cost = payload.get(protocol.PAYLOAD_COST_USD, 0)
    if any(isinstance(n, bool) or not isinstance(n, int | float) or n < 0 for n in (tokens, cost)):
        raise HubError("расход: tokens и cost_usd — неотрицательные числа")
    return int(tokens), float(cost)


def _valid_base_branch(name: str) -> bool:
    if name.startswith(protocol.BASE_BRANCH_PREFIX):
        tail = name[len(protocol.BASE_BRANCH_PREFIX) :]
    elif name.startswith(protocol.AGENT_BRANCH_PREFIX):
        tail = name[len(protocol.AGENT_BRANCH_PREFIX) :]
    elif name.startswith(protocol.EPIC_BRANCH_PREFIX):
        tail = name[len(protocol.EPIC_BRANCH_PREFIX) :]
    else:
        return False
    return bool(tail) and BRANCH_TAIL_RE.match(tail) is not None and ".." not in name


def fix_task_text(ref: str, payload: dict[str, Any]) -> str:
    lines = [f"# fix: красный прогон {ref}", ""]
    for failure in payload.get(protocol.PAYLOAD_FAILURES, []):
        lines.append(f"- {failure[protocol.FAILURE_NAME]}")
        lines.append(f"  {failure[protocol.FAILURE_EXCERPT]}")
    lines += [
        "```",
        payload.get(protocol.PAYLOAD_TAIL, "")[-protocol.FIX_TAIL_CHARS :],
        "```",
        "",
        "Почини упавшие тесты минимальной правкой, не меняя смысла тестов; прогони их и `checks`.",
    ]
    return "\n".join(lines)


def _test_result(payload: dict[str, Any]) -> None:
    exit_code = payload.get(protocol.PAYLOAD_EXIT_CODE)
    duration = payload.get(protocol.PAYLOAD_DURATION_S)
    summary = payload.get(protocol.PAYLOAD_SUMMARY)
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise HubError("test_done: exit_code — целое")
    if isinstance(duration, bool) or not isinstance(duration, int | float) or duration < 0:
        raise HubError("test_done: duration_s — неотрицательное число")
    if not isinstance(summary, dict) or any(
        isinstance(summary.get(key), bool)
        or not isinstance(summary.get(key), int)
        or summary[key] < 0
        for key in protocol.SUMMARY_KEYS
    ):
        raise HubError(f"test_done: summary — неотрицательные целые {protocol.SUMMARY_KEYS}")
    failures = payload.get(protocol.PAYLOAD_FAILURES, [])
    if not isinstance(failures, list) or not all(isinstance(f, dict) for f in failures):
        raise HubError("test_done: failures — список объектов")
    if not isinstance(payload.get(protocol.PAYLOAD_TAIL, ""), str):
        raise HubError("test_done: tail — строка")


def _in(values: Collection[Any]) -> str:
    return ",".join("?" * len(values))


def in_windows(windows: Collection[str], moment: time) -> bool:
    if not windows:
        return True
    for window in windows:
        try:
            start, end = (time.fromisoformat(part) for part in window.split("-", maxsplit=1))
        except ValueError as exc:
            raise ValueError(f"окно стенда {window!r}: ожидается HH:MM-HH:MM") from exc
        if start <= end:
            if start <= moment < end:
                return True
        elif moment >= start or moment < end:
            return True
    return False


class HubQueue:
    def __init__(self, db: Database, logs_root: Path, bench_windows: Collection[str] = ()) -> None:
        self.observers: list[Callable[[str], Awaitable[None]]] = []
        self._db = db
        self.logs_root = logs_root
        self._bench_windows = list(bench_windows)
        in_windows(self._bench_windows, time(0, 0))
        self._lock = asyncio.Lock()
        self._finish_events: dict[str, asyncio.Event] = {}

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock, self._db.transaction() as conn:
            yield conn

    async def add_project(self, name: str) -> str:
        name = name.strip()
        if not PROJECT_NAME_RE.match(name):
            raise HubError("имя проекта: a-z, 0-9, дефис, подчёркивание, до 64 символов")
        async with self._tx() as conn:
            cur = await conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,))
            if await cur.fetchone():
                raise HubError(f"проект {name!r} уже есть")
            await conn.execute(
                "INSERT INTO projects (name, created) VALUES (?, ?)", (name, utc_now().isoformat())
            )
        return name

    async def has_project(self, name: str) -> bool:
        cur = await self._db.conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,))
        return await cur.fetchone() is not None

    async def remove_project(self, name: str) -> None:
        async with self._tx() as conn:
            await conn.execute("DELETE FROM projects WHERE name = ?", (name,))

    async def projects(self) -> list[str]:
        cur = await self._db.conn.execute("SELECT name FROM projects ORDER BY name")
        return [row["name"] for row in await cur.fetchall()]

    @staticmethod
    async def _check_deps(conn: aiosqlite.Connection, client: str, deps: list[str]) -> None:
        for dep in deps:
            cur = await conn.execute(
                "SELECT 1 FROM jobs WHERE id = ? AND client = ?", (dep, client)
            )
            if not await cur.fetchone():
                raise HubError(f"зависимость {dep!r}: задания нет")

    @staticmethod
    async def _insert_agent_job(
        conn: aiosqlite.Connection,
        client: str,
        project: str,
        model: str,
        task: str,
        base_branch: str | None,
        priority: int,
        profile: str,
        purpose: str,
        depends_on: Collection[str] = (),
        fallback: str = "",
        extra_params: dict[str, Any] | None = None,
    ) -> Job:
        job_id = await HubQueue._free_id(conn)
        params_dict = {protocol.PARAM_PROFILE: profile, protocol.PARAM_PURPOSE: purpose}
        deps = list(dict.fromkeys(depends_on))
        if deps:
            params_dict[protocol.PARAM_DEPENDS_ON] = deps
        if fallback:
            params_dict[protocol.PARAM_FALLBACK] = fallback
        if extra_params:
            params_dict.update(extra_params)
        await conn.execute(
            "INSERT INTO jobs (id, kind, project, client, model, task, base_branch, priority,"
            " status, created, params) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                protocol.JOB_KIND_AGENT,
                project,
                client,
                model,
                task,
                base_branch or protocol.BASE_BRANCH_PREFIX + job_id,
                priority,
                protocol.STATUS_QUEUED,
                utc_now().isoformat(),
                json.dumps(params_dict, ensure_ascii=False),
            ),
        )
        return await HubQueue._load(conn, job_id)

    @staticmethod
    async def _maybe_fix_job(
        conn: aiosqlite.Connection, job: Job, payload: dict[str, Any]
    ) -> None:
        fix_model = job.params.get(protocol.PARAM_FIX_MODEL, "")
        round_no = job.params.get(protocol.PARAM_FIX_ROUND, 0)
        if payload.get(protocol.PAYLOAD_EXIT_CODE) != 0 and fix_model:
            ref = job.params[protocol.FIELD_REF]
            if round_no >= protocol.FIX_MAX_ROUNDS:
                await HubQueue._post(
                    conn,
                    job.id,
                    protocol.DIRECTION_TO_CLIENT,
                    protocol.MSG_FIX_EXHAUSTED,
                    {
                        protocol.PAYLOAD_TEXT: (
                            f"fix не справился за {protocol.FIX_MAX_ROUNDS} круга, "
                            f"нужен владелец: {ref}"
                        )
                    },
                )
            else:
                await HubQueue._insert_agent_job(
                    conn,
                    job.client,
                    job.project,
                    fix_model,
                    fix_task_text(ref, payload),
                    ref,
                    0,
                    protocol.FIX_PROFILE,
                    protocol.PURPOSE_FIX,
                    extra_params={
                        protocol.PARAM_FIX_OF: job.id,
                        protocol.PARAM_FIX_ROUND: round_no + 1,
                        protocol.PARAM_FIX_MODEL: fix_model,
                    },
                )

    @staticmethod
    async def _maybe_retest(conn: aiosqlite.Connection, job: Job) -> None:
        if (
            job.kind == protocol.JOB_KIND_AGENT
            and job.params.get(protocol.PARAM_PURPOSE) == protocol.PURPOSE_FIX
            and job.params.get(protocol.PARAM_FIX_MODEL, "")
        ):
            await HubQueue._insert_test_job(
                conn,
                job.client,
                job.project,
                {
                    protocol.FIELD_REF: protocol.AGENT_BRANCH_PREFIX + job.id,
                    protocol.FIELD_CMD: "",
                    protocol.FIELD_ARGS: [],
                    protocol.PARAM_FIX_MODEL: job.params[protocol.PARAM_FIX_MODEL],
                    protocol.PARAM_FIX_ROUND: job.params[protocol.PARAM_FIX_ROUND],
                },
            )

    async def submit(
        self,
        client: str,
        project: str,
        model: str,
        task: str,
        priority: int = 0,
        base_branch: str | None = None,
        profile: str = protocol.DEFAULT_PROFILE,
        purpose: str = protocol.PURPOSE_DEV,
        depends_on: Collection[str] = (),
        fallback: str = "",
    ) -> Job:
        if not model.strip() or not task.strip():
            raise HubError("у задания должны быть модель и task.md")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise HubError("приоритет — целое число")
        if base_branch is not None and not _valid_base_branch(base_branch):
            raise HubError(
                f"базовая ветка: {protocol.BASE_BRANCH_PREFIX}<имя> или "
                f"{protocol.AGENT_BRANCH_PREFIX}<id>, без пробелов и .."
            )
        profile = profile.strip()
        if not PROJECT_NAME_RE.match(profile):
            raise HubError("профиль: латиница, цифры, дефис, подчёркивание")
        if purpose not in protocol.PURPOSES:
            raise HubError(f"назначение: {', '.join(protocol.PURPOSES)}")
        deps = list(dict.fromkeys(depends_on))
        if base_branch is None:
            if len(deps) == 1:
                base_branch = protocol.AGENT_BRANCH_PREFIX + deps[0]
            elif len(deps) > 1:
                raise HubError("несколько зависимостей: укажи base_branch")
        fallback = fallback.strip()
        if fallback and fallback == model.strip():
            raise HubError("запасная модель совпадает с основной")
        async with self._tx() as conn:
            cur = await conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,))
            if not await cur.fetchone():
                raise HubError(f"проекта {project!r} нет")
            await self._check_deps(conn, client, deps)
            return await HubQueue._insert_agent_job(
                conn,
                client,
                project,
                model.strip(),
                task,
                base_branch,
                priority,
                profile,
                purpose,
                deps,
                fallback,
            )

    async def submit_test(
        self,
        client: str,
        project: str,
        ref: str,
        cmd: str = "",
        args: list[str] | None = None,
        depends_on: Collection[str] = (),
        fix_model: str = "",
        fix_round: int = 0,
    ) -> Job:
        args = args or []
        deps = list(dict.fromkeys(depends_on))
        if not ref.strip() and len(deps) == 1:
            ref = protocol.AGENT_BRANCH_PREFIX + deps[0]
        if not ref.strip() or ref.startswith("-") or any(c.isspace() for c in ref):
            raise HubError("ref: ветка или коммит без пробелов, не с «-»")
        fix_model = fix_model.strip()
        if fix_model and not _valid_base_branch(ref):
            raise HubError(
                "fix_model: ref прогона должен быть веткой base/<имя> или agent/<id>"
            )
        params = {
            protocol.FIELD_REF: ref,
            protocol.FIELD_CMD: cmd.strip(),
            protocol.FIELD_ARGS: args,
        }
        if fix_model:
            params[protocol.PARAM_FIX_MODEL] = fix_model
        if fix_round > 0:
            params[protocol.PARAM_FIX_ROUND] = fix_round
        async with self._tx() as conn:
            cur = await conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,))
            if not await cur.fetchone():
                raise HubError(f"проекта {project!r} нет")
            await self._check_deps(conn, client, deps)
            if deps:
                params[protocol.PARAM_DEPENDS_ON] = deps
            return await HubQueue._insert_test_job(conn, client, project, params)

    @staticmethod
    async def _insert_test_job(
        conn: aiosqlite.Connection, client: str, project: str, params: dict[str, Any]
    ) -> Job:
        job_id = await HubQueue._free_id(conn)
        await conn.execute(
            "INSERT INTO jobs (id, kind, project, client, model, task, base_branch, priority,"
            " status, created, params) VALUES (?, ?, ?, ?, '', '', '', 0, ?, ?, ?)",
            (
                job_id,
                protocol.JOB_KIND_TEST,
                project,
                client,
                protocol.STATUS_QUEUED,
                utc_now().isoformat(),
                json.dumps(params, ensure_ascii=False),
            ),
        )
        return await HubQueue._load(conn, job_id)

    async def wait_terminal(self, job_id: str, timeout: float) -> Job:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            event = self._finish_events.setdefault(job_id, asyncio.Event())
            job = await self._load(self._db.conn, job_id)
            remaining = deadline - loop.time()
            if job.status in protocol.TERMINAL_STATUSES or remaining <= 0:
                return job
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), remaining)

    def _wake_waiters(self, job_id: str) -> None:
        event = self._finish_events.pop(job_id, None)
        if event is not None:
            event.set()

    async def last_message(self, job_id: str, kind: str) -> Message | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM messages WHERE job_id = ? AND direction = ? AND kind = ?"
            " ORDER BY seq DESC LIMIT 1",
            (job_id, protocol.DIRECTION_TO_CLIENT, kind),
        )
        row = await cur.fetchone()
        return _message(row) if row else None

    async def step_stats(self, job_ids: Collection[str]) -> dict[str, tuple[int, str]]:
        if not job_ids:
            return {}
        placeholders = _in(job_ids)
        cur = await self._db.conn.execute(
            f"SELECT job_id, COUNT(*) as cnt, MAX(seq) as max_seq "
            f"FROM messages WHERE job_id IN ({placeholders}) AND direction = ? AND kind = ? "
            f"GROUP BY job_id",
            (*job_ids, protocol.DIRECTION_TO_CLIENT, protocol.MSG_STEP_DONE),
        )
        rows = await cur.fetchall()
        if not rows:
            return {}
        job_ids_with_steps = [row["job_id"] for row in rows]
        placeholders2 = _in(job_ids_with_steps)
        cur = await self._db.conn.execute(
            f"SELECT job_id, payload FROM messages "
            f"WHERE job_id IN ({placeholders2}) AND direction = ? AND kind = ? "
            f"AND seq IN (SELECT MAX(seq) FROM messages WHERE job_id IN ({placeholders2}) "
            f"AND direction = ? AND kind = ? GROUP BY job_id)",
            (
                *job_ids_with_steps,
                protocol.DIRECTION_TO_CLIENT,
                protocol.MSG_STEP_DONE,
                *job_ids_with_steps,
                protocol.DIRECTION_TO_CLIENT,
                protocol.MSG_STEP_DONE,
            ),
        )
        last_steps = {
            row["job_id"]: json.loads(row["payload"]).get(protocol.PAYLOAD_TEXT, "")
            for row in await cur.fetchall()
        }
        return {row["job_id"]: (row["cnt"], last_steps.get(row["job_id"], "")) for row in rows}

    async def job_statuses(self, job_ids: Collection[str]) -> dict[str, str]:
        if not job_ids:
            return {}
        placeholders = _in(job_ids)
        cur = await self._db.conn.execute(
            f"SELECT id, status FROM jobs WHERE id IN ({placeholders})", tuple(job_ids)
        )
        return {row["id"]: row["status"] for row in await cur.fetchall()}

    async def slot_waiting(self, job_ids: Collection[str]) -> set[str]:
        if not job_ids:
            return set()
        placeholders = _in(job_ids)
        cur = await self._db.conn.execute(
            f"SELECT m1.job_id "
            f"FROM messages m1 "
            f"JOIN jobs j ON j.id = m1.job_id "
            f"WHERE m1.job_id IN ({placeholders}) "
            f"AND m1.direction = ? AND m1.kind = ? "
            f"AND m1.seq > COALESCE(("
            f" SELECT MAX(m2.seq) FROM messages m2 "
            f" WHERE m2.job_id = m1.job_id "
            f" AND m2.direction = ? AND m2.kind = ?"
            f"), 0) "
            f"AND j.status = ? AND j.kind = ?",
            (
                *job_ids,
                protocol.DIRECTION_TO_EXECUTOR,
                protocol.MSG_ANSWER,
                protocol.DIRECTION_TO_CLIENT,
                protocol.MSG_STARTED,
                protocol.STATUS_RUNNING,
                protocol.JOB_KIND_AGENT,
            ),
        )
        rows = await cur.fetchall()
        return {row["job_id"] for row in rows}

    async def get_job(self, job_id: str, client: str | None = None) -> Job:
        job = await self._load(self._db.conn, job_id)
        if client is not None and job.client != client:
            raise HubError(f"задания {job_id!r} нет")
        return job

    async def list_jobs(
        self, client: str | None = None, statuses: Collection[str] | None = None
    ) -> list[Job]:
        where: list[str] = []
        args: list[Any] = []
        if client is not None:
            where.append("client = ?")
            args.append(client)
        if statuses is not None:
            where.append(f"status IN ({_in(statuses)})")
            args.extend(statuses)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        cur = await self._db.conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs{clause} ORDER BY seq", args
        )
        return [_job(row) for row in await cur.fetchall()]

    async def active_jobs(self, offset: int, limit: int) -> tuple[list[Job], int]:
        where = f"WHERE status IN ({_in(protocol.ACTIVE_STATUSES)}, ?)"
        order = (
            f" ORDER BY CASE status WHEN ? THEN 0 WHEN ? THEN 0 ELSE 1 END,"
            f" CASE WHEN status IN ({_in(protocol.ACTIVE_STATUSES)}) THEN started END,"
            f" {QUEUE_ORDER_SQL}"
        )
        cur = await self._db.conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs {where}{order} LIMIT ? OFFSET ?",
            (
                *protocol.ACTIVE_STATUSES,
                protocol.STATUS_QUEUED,
                *protocol.ACTIVE_STATUSES,
                *protocol.ACTIVE_STATUSES,
                *QUEUE_ORDER_ARGS,
                limit,
                offset,
            ),
        )
        jobs = [_job(row) for row in await cur.fetchall()]
        cur = await self._db.conn.execute(
            f"SELECT COUNT(*) FROM jobs {where}",
            (*protocol.ACTIVE_STATUSES, protocol.STATUS_QUEUED),
        )
        row = await cur.fetchone()
        return jobs, row[0]

    async def claim(
        self,
        executor: str,
        models: Collection[str],
        kinds: Collection[str] = (protocol.JOB_KIND_AGENT,),
        busy_bases: Collection[str] = (),
        live_models: Collection[str] | None = None,
        now: datetime | None = None,
    ) -> Job | None:
        unknown = [k for k in kinds if k not in protocol.JOB_KINDS]
        if unknown:
            raise HubError(f"виды {unknown} неизвестны, есть: {', '.join(protocol.JOB_KINDS)}")
        fits: list[str] = []
        args: list[Any] = [protocol.STATUS_QUEUED]
        if protocol.JOB_KIND_TEST in kinds:
            fits.append("kind = ?")
            args.append(protocol.JOB_KIND_TEST)
        if protocol.JOB_KIND_AGENT in kinds and models:
            free_base = f" AND base_branch NOT IN ({_in(busy_bases)})" if busy_bases else ""
            fits.append(f"(kind = ? AND model IN ({_in(models)}){free_base})")
            args.extend([protocol.JOB_KIND_AGENT, *models, *busy_bases])
            busy = "EXISTS (SELECT 1 FROM jobs r WHERE r.status = ? AND r.model = jobs.model)"
            live = ""
            if live_models is not None:
                live = f" OR model NOT IN ({_in(live_models)})" if live_models else " OR 1 = 1"
            fits.append(
                f"(kind = ? AND json_extract(params, '$.fallback') IN ({_in(models)})"
                f" AND ({busy}{live}){free_base})"
            )
            args.extend([protocol.JOB_KIND_AGENT, *models, protocol.STATUS_RUNNING])
            if live_models:
                args.extend(live_models)
            args.extend(busy_bases)
        if not fits:
            return None
        args.append(protocol.STATUS_DONE)
        bench, bench_args = self._bench_filter(now)
        async with self._tx() as conn:
            cur = await conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE status = ? AND "
                f"({' OR '.join(fits)}) AND {READY_SQL}{bench}"
                f" ORDER BY {QUEUE_ORDER_SQL} LIMIT 1",
                (
                    *args,
                    *bench_args,
                    *QUEUE_ORDER_ARGS,
                ),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            job = _job(row)
            now_dt = utc_now()
            self._check_transition(job, protocol.STATUS_RUNNING)
            if protocol.PARAM_FALLBACK in job.params and job.model not in models:
                params = {**job.params, protocol.PARAM_ORIGINAL_MODEL: job.model}
                await conn.execute(
                    "UPDATE jobs SET model = ?, params = ? WHERE id = ?",
                    (
                        job.params[protocol.PARAM_FALLBACK],
                        json.dumps(params, ensure_ascii=False),
                        job.id,
                    ),
                )
            await conn.execute(
                "UPDATE jobs SET status = ?, executor = ?, started = COALESCE(started, ?),"
                " finished = NULL, active_seconds = ?, active_since = ? WHERE id = ?",
                (
                    protocol.STATUS_RUNNING,
                    executor,
                    now_dt.isoformat(),
                    *HubQueue._active_after(job, protocol.STATUS_RUNNING, now_dt),
                    job.id,
                ),
            )
            return await self._load(conn, job.id)

    async def post_from_executor(
        self,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        executor: str | None = None,
    ) -> Message:
        payload = payload or {}
        if kind not in protocol.EXECUTOR_MESSAGES:
            raise HubError(
                f"вид {kind!r} не от исполнителя, есть: {', '.join(protocol.EXECUTOR_MESSAGES)}"
            )
        tokens, cost = _spend(payload)
        if kind == protocol.MSG_TEST_DONE:
            _test_result(payload)
        async with self._tx() as conn:
            job = await self._load(conn, job_id)
            if executor is not None and job.executor != executor:
                raise HubError(f"задание {job_id} ведёт не {executor!r}")
            is_test = job.kind == protocol.JOB_KIND_TEST
            if (kind == protocol.MSG_TEST_DONE and not is_test) or (
                kind == protocol.MSG_QUESTION and is_test
            ):
                raise HubError(f"задание {job_id} ({job.kind}): {kind} недопустим")
            if kind == protocol.MSG_REQUEUE:
                if job.status != protocol.STATUS_RUNNING:
                    raise HubError(
                        f"задание {job_id} ({job.kind}, {job.status}): {kind} недопустим"
                    )
                kind, payload = await self._requeue(conn, job, payload)
            elif not (
                (
                    kind == protocol.MSG_STOPPED
                    and job.status in (protocol.STATUS_STOPPED, protocol.STATUS_QUEUED)
                )
                or (kind == protocol.MSG_QUESTION and job.status == protocol.STATUS_WAITING_OWNER)
            ):
                target = protocol.MESSAGE_TARGET_STATUS[kind]
                if target is None:
                    if job.status != protocol.STATUS_RUNNING:
                        raise HubError(
                            f"задание {job_id}: {kind} в статусе {job.status} невозможен"
                        )
                else:
                    self._check_transition(job, target)
                    await self._set_status(conn, job, target)
                    if target in (protocol.STATUS_FAILED, protocol.STATUS_STOPPED):
                        await HubQueue._notify_dependents(conn, job, target)
            if kind == protocol.MSG_TEST_DONE:
                await HubQueue._maybe_fix_job(conn, job, payload)
            if kind == protocol.MSG_DONE:
                await HubQueue._maybe_retest(conn, job)
            await conn.execute(
                "UPDATE jobs SET tokens = tokens + ?, cost_usd = cost_usd + ? WHERE id = ?",
                (tokens, cost, job_id),
            )
            message = await self._post(conn, job_id, protocol.DIRECTION_TO_CLIENT, kind, payload)
        self._wake_waiters(job_id)
        await self._observe(job_id)
        return message

    async def _observe(self, job_id: str) -> None:
        for observer in self.observers:
            try:
                await observer(job_id)
            except Exception:
                log.exception("Наблюдатель задания %s упал", job_id)

    async def notify_client(self, job_id: str, kind: str, payload: dict[str, Any]) -> Message:
        async with self._tx() as conn:
            await self._load(conn, job_id)
            message = await self._post(conn, job_id, protocol.DIRECTION_TO_CLIENT, kind, payload)
        self._wake_waiters(job_id)
        return message

    async def _requeue(
        self, conn: aiosqlite.Connection, job: Job, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        attempts = int(job.params.get(protocol.PARAM_ATTEMPTS, 0))
        limit = (
            protocol.TEST_MAX_REQUEUES
            if job.kind == protocol.JOB_KIND_TEST
            else protocol.AGENT_MAX_REQUEUES
        )
        if attempts >= limit:
            self._check_transition(job, protocol.STATUS_FAILED)
            await self._set_status(conn, job, protocol.STATUS_FAILED)
            await HubQueue._notify_dependents(conn, job, protocol.STATUS_FAILED)
            if job.kind == protocol.JOB_KIND_TEST:
                text = f"исполнитель перезапускался во время прогона {attempts + 1} раз"
            else:
                text = f"модель недоступна, задание возвращалось в очередь {attempts + 1} раз"
            return protocol.MSG_FAILED, {protocol.PAYLOAD_TEXT: text}
        params = {**job.params, protocol.PARAM_ATTEMPTS: attempts + 1}
        await conn.execute(
            "UPDATE jobs SET status = ?, executor = NULL, finished = NULL, params = ?,"
            " active_seconds = ?, active_since = ? WHERE id = ?",
            (
                protocol.STATUS_QUEUED,
                json.dumps(params, ensure_ascii=False),
                *HubQueue._active_after(job, protocol.STATUS_QUEUED, utc_now()),
                job.id,
            ),
        )
        return protocol.MSG_REQUEUE, payload

    async def post_from_client(
        self,
        client: str,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        observe: bool = True,
    ) -> Message:
        payload = payload or {}
        if kind not in protocol.CLIENT_MESSAGES:
            raise HubError(
                f"вид {kind!r} не от клиента, есть: {', '.join(protocol.CLIENT_MESSAGES)}"
            )
        async with self._tx() as conn:
            job = await self._load(conn, job_id)
            if job.client != client:
                raise HubError(f"задание {job_id} чужое")
            if kind == protocol.MSG_ANSWER:
                self._check_transition(job, protocol.STATUS_RUNNING)
                await self._set_status(conn, job, protocol.STATUS_RUNNING)
            elif kind == protocol.MSG_RESUME:
                self._check_transition(job, protocol.STATUS_QUEUED)
                model = payload.get(protocol.PAYLOAD_MODEL)
                if model is not None and (not isinstance(model, str) or not model.strip()):
                    raise HubError("model: непустая строка")
                await self._set_status(conn, job, protocol.STATUS_QUEUED)
                text = payload.get(protocol.PAYLOAD_TEXT)
                remarks = text.strip() if isinstance(text, str) else ""
                params = {
                    k: v
                    for k, v in job.params.items()
                    if k not in (protocol.PARAM_REMARKS, protocol.PARAM_ATTEMPTS)
                }
                if remarks:
                    params[protocol.PARAM_REMARKS] = remarks
                await conn.execute(
                    "UPDATE jobs SET params = ? WHERE id = ?",
                    (json.dumps(params, ensure_ascii=False), job_id),
                )
                if model is not None:
                    await conn.execute(
                        "UPDATE jobs SET model = ? WHERE id = ?", (model.strip(), job_id)
                    )
            elif kind == protocol.MSG_STOP:
                self._check_transition(job, protocol.STATUS_STOPPED)
                await self._set_status(conn, job, protocol.STATUS_STOPPED)
                await HubQueue._notify_dependents(conn, job, protocol.STATUS_STOPPED)
                if job.status == protocol.STATUS_QUEUED:
                    await self._post(
                        conn, job_id, protocol.DIRECTION_TO_CLIENT, protocol.MSG_STOPPED, {}
                    )
            elif job.status != protocol.STATUS_WAITING_OWNER:
                raise HubError(f"release только для задания, ждущего ответа, сейчас {job.status}")
            message = await self._post(conn, job_id, protocol.DIRECTION_TO_EXECUTOR, kind, payload)
        self._wake_waiters(job_id)
        if observe:
            await self._observe(job_id)
        return message

    async def add_review(
        self,
        client: str,
        job_id: str,
        verdict: str,
        issues: list[dict[str, str]],
        fix_commit: str = "",
        tests: str = "",
    ) -> dict[str, Any]:
        if verdict not in protocol.REVIEW_VERDICTS:
            raise HubError(
                f"вердикт: {', '.join(protocol.REVIEW_VERDICTS)}"
            )
        if not isinstance(issues, list):
            raise HubError("issues: список объектов с tag и note")
        for issue in issues:
            if not isinstance(issue, dict):
                raise HubError("issues: список объектов с tag и note")
            tag = issue.get(protocol.FIELD_TAG)
            note = issue.get(protocol.FIELD_NOTE)
            if tag not in protocol.REVIEW_ISSUE_TAGS:
                raise HubError(
                    f"метка: {', '.join(protocol.REVIEW_ISSUE_TAGS)}"
                )
            if not isinstance(note, str):
                raise HubError("issues: список объектов с tag и note")
        issues_json = json.dumps(issues, ensure_ascii=False)
        async with self._tx() as conn:
            cur = await conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
            if not await cur.fetchone():
                raise HubError(f"задания {job_id!r} нет")
            await conn.execute(
                "INSERT INTO reviews (job_id, verdict, issues, fix_commit, tests, client, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(job_id) DO UPDATE SET verdict = excluded.verdict,"
                " issues = excluded.issues, fix_commit = excluded.fix_commit,"
                " tests = excluded.tests, client = excluded.client, created = excluded.created",
                (job_id, verdict, issues_json, fix_commit, tests, client, utc_now().isoformat()),
            )
            row = await self._select_reviews(conn, "WHERE r.job_id = ?", (job_id,))
            if not row:
                raise HubError(f"оценка {job_id!r} не записалась")
            return self._review_view(row[0])

    @staticmethod
    def _review_view(row: aiosqlite.Row) -> dict[str, Any]:
        issues = json.loads(row["issues"])
        return {
            protocol.FIELD_ID: row["id"],
            protocol.FIELD_MODEL: row["model"],
            protocol.PARAM_PROFILE: row["profile"],
            protocol.FIELD_TITLE: row["title"],
            protocol.FIELD_BASE_BRANCH: row["base_branch"],
            protocol.PAYLOAD_TOKENS: row["tokens"],
            protocol.FIELD_RESUMES: row["resumes"],
            protocol.FIELD_VERDICT: row["verdict"],
            protocol.FIELD_ISSUES: issues,
            protocol.FIELD_FIX_COMMIT: row["fix_commit"],
            protocol.FIELD_TESTS: row["tests"],
            protocol.FIELD_REVIEWED: row["reviewed"],
        }

    async def _select_reviews(
        self, conn: aiosqlite.Connection, where: str, args: tuple[Any, ...]
    ) -> list[aiosqlite.Row]:
        sql = f"""
            SELECT
                j.id,
                j.model,
                json_extract(j.params, '$.{protocol.PARAM_PROFILE}') AS profile,
                TRIM(
                    SUBSTR(j.task, 1,
                        CASE
                            WHEN INSTR(j.task, '\n') > 0 THEN INSTR(j.task, '\n') - 1
                            ELSE LENGTH(j.task)
                        END
                    ),
                    '# '
                ) AS title,
                j.base_branch,
                j.tokens,
                (
                    SELECT COUNT(*)
                    FROM messages m
                    WHERE m.job_id = j.id
                      AND m.direction = ?
                      AND m.kind = ?
                ) AS resumes,
                r.verdict,
                r.issues,
                r.fix_commit,
                r.tests,
                r.created AS reviewed
            FROM reviews r
            JOIN jobs j ON j.id = r.job_id
            {where}
            ORDER BY r.created
        """
        cur = await conn.execute(
            sql, (protocol.DIRECTION_TO_EXECUTOR, protocol.MSG_RESUME, *args)
        )
        return await cur.fetchall()

    async def reviews(
        self, model: str = "", since: str = ""
    ) -> list[dict[str, Any]]:
        where = ""
        args: list[Any] = []
        if model:
            where = "WHERE j.model = ?"
            args.append(model)
        if since:
            if where:
                where += " AND r.created >= ?"
            else:
                where = "WHERE r.created >= ?"
            args.append(since)
        async with self._tx() as conn:
            rows = await self._select_reviews(conn, where, tuple(args))
            return [self._review_view(row) for row in rows]

    async def inbox(self, client: str, since: int = 0, limit: int = DEFAULT_PAGE) -> list[Message]:
        cur = await self._db.conn.execute(
            "SELECT m.* FROM messages m JOIN jobs j ON j.id = m.job_id"
            " WHERE j.client = ? AND m.direction = ? AND m.seq > ? ORDER BY m.seq LIMIT ?",
            (client, protocol.DIRECTION_TO_CLIENT, since, limit),
        )
        return [_message(row) for row in await cur.fetchall()]

    async def max_seq(self) -> int:
        cur = await self._db.conn.execute("SELECT COALESCE(MAX(seq), 0) AS n FROM messages")
        row = await cur.fetchone()
        return row["n"] if row else 0

    async def inbox_all(
        self, since: int = 0, limit: int = DEFAULT_PAGE
    ) -> list[tuple[Message, Job]]:
        cur = await self._db.conn.execute(
            "SELECT * FROM messages WHERE direction = ? AND seq > ? ORDER BY seq LIMIT ?",
            (protocol.DIRECTION_TO_CLIENT, since, limit),
        )
        messages = [_message(row) for row in await cur.fetchall()]
        jobs: dict[str, Job] = {}
        for message in messages:
            if message.job_id not in jobs:
                jobs[message.job_id] = await self._load(self._db.conn, message.job_id)
        return [(message, jobs[message.job_id]) for message in messages]

    async def ack(self, client: str, upto: int) -> int:
        async with self._tx() as conn:
            cur = await conn.execute(
                "UPDATE messages SET read_at = ? WHERE direction = ? AND seq <= ?"
                " AND read_at IS NULL"
                " AND job_id IN (SELECT id FROM jobs WHERE client = ?)",
                (utc_now().isoformat(), protocol.DIRECTION_TO_CLIENT, upto, client),
            )
            return cur.rowcount

    async def poll(
        self,
        since: int = 0,
        job_id: str | None = None,
        executor: str | None = None,
        limit: int = DEFAULT_PAGE,
    ) -> list[Message]:
        if job_id is None and executor is None:
            raise HubError("poll: нужен job_id или executor")
        where: list[str] = []
        args: list[Any] = []
        if job_id is not None:
            where.append("m.job_id = ?")
            args.append(job_id)
        if executor is not None:
            where.append("j.executor = ?")
            args.append(executor)
        where.append(
            "NOT (m.kind = ? AND EXISTS (SELECT 1 FROM messages r "
            "WHERE r.job_id = m.job_id AND r.direction = ? AND r.kind = ? AND r.seq > m.seq))"
        )
        args.extend([protocol.MSG_STOP, protocol.DIRECTION_TO_EXECUTOR, protocol.MSG_RESUME])
        async with self._tx() as conn:
            cur = await conn.execute(
                "SELECT m.* FROM messages m JOIN jobs j ON j.id = m.job_id"
                f" WHERE m.direction = ? AND m.seq > ? AND {' AND '.join(where)}"
                " ORDER BY m.seq LIMIT ?",
                (protocol.DIRECTION_TO_EXECUTOR, since, *args, limit),
            )
            messages = [_message(row) for row in await cur.fetchall()]
            if messages:
                await conn.execute(
                    "UPDATE messages SET read_at = COALESCE(read_at, ?)"
                    f" WHERE seq IN ({_in(messages)})",
                    (utc_now().isoformat(), *(m.seq for m in messages)),
                )
            return messages

    async def queued_count(self) -> int:
        cur = await self._db.conn.execute(
            "SELECT count(*) AS n FROM jobs WHERE status = ?", (protocol.STATUS_QUEUED,)
        )
        row = await cur.fetchone()
        return row["n"] if row else 0

    async def local_demand(self, models: set[str]) -> bool:
        if not models:
            return False
        cur = await self._db.conn.execute(
            "SELECT 1 FROM jobs WHERE status IN (?, ?) AND model IN ("
            f"{_in(models)}) LIMIT 1",
            (protocol.STATUS_QUEUED, protocol.STATUS_RUNNING, *models),
        )
        return await cur.fetchone() is not None

    def _bench_filter(self, now: datetime | None) -> tuple[str, list[Any]]:
        if in_windows(self._bench_windows, (now or datetime.now()).time()):
            return "", []
        return (
            " AND COALESCE(json_extract(params, '$.purpose'), '') != ?",
            [protocol.PURPOSE_BENCH],
        )

    async def pending_work(self, now: datetime | None = None) -> bool:
        bench, bench_args = self._bench_filter(now)
        cur = await self._db.conn.execute(
            f"SELECT 1 FROM jobs WHERE status = ? AND "
            f"kind IN ({_in(protocol.JOB_KINDS)}) AND {READY_SQL}{bench}"
            " UNION ALL"
            " SELECT 1 FROM messages m JOIN jobs j ON j.id = m.job_id"
            " WHERE m.direction = ? AND m.read_at IS NULL AND m.kind != ?"
            " AND j.executor IS NOT NULL"
            " LIMIT 1",
            (
                protocol.STATUS_QUEUED,
                *protocol.JOB_KINDS,
                protocol.STATUS_DONE,
                *bench_args,
                protocol.DIRECTION_TO_EXECUTOR,
                protocol.MSG_RELEASE,
            ),
        )
        return await cur.fetchone() is not None

    async def note_progress(self, progress: Any, now: datetime | None = None) -> None:
        if not isinstance(progress, dict):
            return
        stamp = (now or utc_now()).isoformat()
        async with self._tx() as conn:
            for job_id, counter in progress.items():
                if isinstance(counter, bool) or not isinstance(counter, int):
                    continue
                cur = await conn.execute(
                    "SELECT counter FROM job_progress WHERE job_id = ?", (job_id,)
                )
                row = await cur.fetchone()
                if row is not None and row["counter"] == counter:
                    continue
                cur = await conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
                if await cur.fetchone() is None:
                    continue
                await conn.execute(
                    "INSERT INTO job_progress (job_id, counter, changed) VALUES (?, ?, ?)"
                    " ON CONFLICT(job_id) DO UPDATE SET counter = excluded.counter,"
                    " changed = excluded.changed",
                    (job_id, counter, stamp),
                )

    async def note_live(self, live: Any, now: datetime | None = None) -> None:
        if not isinstance(live, dict):
            return
        stamp = (now or utc_now()).isoformat()
        async with self._tx() as conn:
            for job_id, entry in live.items():
                if not isinstance(entry, dict):
                    continue
                cur = await conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
                if await cur.fetchone() is None:
                    continue
                data = {key: entry[key] for key in LIVE_KEYS if key in entry}
                await conn.execute(
                    "INSERT INTO job_live (job_id, data, changed) VALUES (?, ?, ?)"
                    " ON CONFLICT(job_id) DO UPDATE SET data = excluded.data,"
                    " changed = excluded.changed",
                    (job_id, json.dumps(data), stamp),
                )

    async def live_views(self, job_ids: Collection[str]) -> dict[str, dict[str, Any]]:
        if not job_ids:
            return {}
        cur = await self._db.conn.execute(
            f"SELECT job_id, data FROM job_live WHERE job_id IN ({_in(job_ids)})", tuple(job_ids)
        )
        return {row["job_id"]: json.loads(row["data"]) for row in await cur.fetchall()}

    async def flag_stalled(
        self, threshold_s: float, executor_alive: bool, now: datetime | None = None
    ) -> list[Message]:
        now = now or utc_now()
        flagged: list[Message] = []
        async with self._tx() as conn:
            cur = await conn.execute(
                "SELECT j.id, j.started, ("
                " SELECT MAX(created) FROM messages WHERE job_id = j.id AND kind != ?) AS news,"
                " (SELECT MAX(created) FROM messages WHERE job_id = j.id AND kind = ?) AS flagged,"
                " (SELECT changed FROM job_progress WHERE job_id = j.id) AS progress"
                " FROM jobs j WHERE j.status = ? AND j.kind = ?",
                (
                    protocol.MSG_STALLED,
                    protocol.MSG_STALLED,
                    protocol.STATUS_RUNNING,
                    protocol.JOB_KIND_AGENT,
                ),
            )
            rows = await cur.fetchall()
            for row in rows:
                stamps = [s for s in (row["started"], row["news"], row["progress"]) if s]
                if not stamps:
                    continue
                last = max(datetime.fromisoformat(s) for s in stamps)
                silent = (now - last).total_seconds()
                if silent < threshold_s:
                    continue
                if row["flagged"] and datetime.fromisoformat(row["flagged"]) > last:
                    continue
                payload = {
                    protocol.PAYLOAD_SILENT_MIN: int(silent // 60),
                    protocol.PAYLOAD_EXECUTOR_ALIVE: executor_alive,
                }
                flagged.append(
                    await self._post(
                        conn, row["id"], protocol.DIRECTION_TO_CLIENT, protocol.MSG_STALLED, payload
                    )
                )
        return flagged

    async def flag_waiting(
        self,
        threshold_s: float,
        executor_alive: bool,
        live_models: Collection[str],
        now: datetime | None = None,
    ) -> list[Message]:
        now = now or utc_now()
        flagged: list[Message] = []
        async with self._tx() as conn:
            cur = await conn.execute(
                "SELECT id, model, params, created FROM jobs "
                f"WHERE status = ? AND kind = ? AND {READY_SQL} "
                "ORDER BY seq",
                (protocol.STATUS_QUEUED, protocol.JOB_KIND_AGENT, protocol.STATUS_DONE),
            )
            rows = await cur.fetchall()
            for row in rows:
                created = datetime.fromisoformat(row["created"])
                waited = (now - created).total_seconds()
                if waited < threshold_s:
                    continue
                model = row["model"]
                params = json.loads(row["params"])
                fallback = params.get(protocol.PARAM_FALLBACK, "")
                if not executor_alive:
                    kind = protocol.MSG_EXECUTOR_OFFLINE
                elif model not in live_models and fallback not in live_models:
                    kind = protocol.MSG_WAITING_MODEL
                else:
                    continue
                cur = await conn.execute(
                    "SELECT 1 FROM messages WHERE job_id = ? AND kind = ?",
                    (row["id"], kind),
                )
                if await cur.fetchone():
                    continue
                payload = {protocol.PAYLOAD_WAITED_MIN: int(waited // 60)}
                flagged.append(
                    await self._post(
                        conn, row["id"], protocol.DIRECTION_TO_CLIENT, kind, payload
                    )
                )
        return flagged

    async def purge(self, retention_days: int, now: datetime | None = None) -> int:
        border = ((now or utc_now()) - timedelta(days=retention_days)).isoformat()
        async with self._tx() as conn:
            cur = await conn.execute(
                "DELETE FROM messages WHERE read_at IS NOT NULL AND read_at < ?", (border,)
            )
            removed = cur.rowcount
            await conn.execute(
                "DELETE FROM job_progress WHERE job_id IN"
                " (SELECT id FROM jobs WHERE finished IS NOT NULL AND finished < ?)",
                (border,),
            )
            await conn.execute(
                "DELETE FROM job_live WHERE job_id IN"
                " (SELECT id FROM jobs WHERE finished IS NOT NULL AND finished < ?)",
                (border,),
            )
            cur = await conn.execute(
                "SELECT id FROM jobs WHERE kind = ? AND finished IS NOT NULL AND finished < ?",
                (protocol.JOB_KIND_TEST, border),
            )
            expired = [row["id"] for row in await cur.fetchall()]
        for job_id in expired:
            joblogs.remove(self.logs_root, job_id)
        return removed

    @staticmethod
    def _check_transition(job: Job, target: str) -> None:
        if target not in protocol.TRANSITIONS[job.status]:
            raise TransitionRefused(
                f"задание {job.id}: переход {job.status} → {target} недопустим", job.status
            )

    @staticmethod
    async def _set_status(conn: aiosqlite.Connection, job: Job, target: str) -> None:
        now = utc_now()
        finished = now.isoformat() if target in protocol.TERMINAL_STATUSES else None
        await conn.execute(
            "UPDATE jobs SET status = ?, finished = ?, active_seconds = ?, active_since = ?"
            " WHERE id = ?",
            (target, finished, *HubQueue._active_after(job, target, now), job.id),
        )

    @staticmethod
    def _active_after(job: Job, target: str, now: datetime) -> tuple[float, str | None]:
        if target == protocol.STATUS_RUNNING:
            if job.status == protocol.STATUS_RUNNING and job.active_since:
                return job.active_seconds, job.active_since
            return job.active_seconds, now.isoformat()
        if job.status == protocol.STATUS_RUNNING:
            return _closed(job, now), None
        return job.active_seconds, None

    @staticmethod
    async def _notify_dependents(
        conn: aiosqlite.Connection, job: Job, status: str
    ) -> None:
        cur = await conn.execute(
            "SELECT j.id FROM jobs j, json_each(j.params, '$.depends_on') d "
            "WHERE j.status = ? AND j.client = ? AND d.value = ?",
            (protocol.STATUS_QUEUED, job.client, job.id),
        )
        dependent_ids = [row["id"] for row in await cur.fetchall()]
        for dep_id in dependent_ids:
            await HubQueue._post(
                conn,
                dep_id,
                protocol.DIRECTION_TO_CLIENT,
                protocol.MSG_DEPENDENCY_FAILED,
                {
                    protocol.PAYLOAD_DEPENDENCY: job.id,
                    protocol.PAYLOAD_DEPENDENCY_STATUS: status,
                },
            )

    @staticmethod
    async def _post(
        conn: aiosqlite.Connection, job_id: str, direction: str, kind: str, payload: dict[str, Any]
    ) -> Message:
        now = utc_now().isoformat()
        cur = await conn.execute(
            "INSERT INTO messages (job_id, direction, kind, payload, created)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, direction, kind, json.dumps(payload, ensure_ascii=False), now),
        )
        return Message(cur.lastrowid or 0, job_id, direction, kind, payload, now, False)

    @staticmethod
    async def _load(conn: aiosqlite.Connection, job_id: str) -> Job:
        cur = await conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,))
        row = await cur.fetchone()
        if row is None:
            raise HubError(f"задания {job_id!r} нет")
        return _job(row)

    @staticmethod
    async def _free_id(conn: aiosqlite.Connection) -> str:
        for _ in range(JOB_ID_ATTEMPTS):
            job_id = "".join(secrets.choice(JOB_ID_ALPHABET) for _ in range(JOB_ID_LENGTH))
            cur = await conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,))
            if not await cur.fetchone():
                return job_id
        raise HubError("не удалось подобрать свободный id задания")
