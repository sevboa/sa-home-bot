from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from sa_home_bot.db.connection import Database
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.utils.clock import utc_now

PROJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
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
    cost_usd REAL NOT NULL DEFAULT 0
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
"""

JOB_COLUMNS = (
    "id, kind, project, client, model, task, base_branch, priority, status, executor, "
    "created, started, finished, tokens, cost_usd"
)


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
    return Job(**{key: row[key] for key in row.keys()})


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


def _in(values: Collection[Any]) -> str:
    return ",".join("?" * len(values))


class HubQueue:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock, self._db.transaction() as conn:
            yield conn

    async def add_project(self, name: str) -> str:
        name = name.strip()
        if not PROJECT_NAME_RE.match(name):
            raise HubError("имя проекта: a-z, 0-9, точка, дефис, подчёркивание, до 64 символов")
        async with self._tx() as conn:
            cur = await conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,))
            if await cur.fetchone():
                raise HubError(f"проект {name!r} уже есть")
            await conn.execute(
                "INSERT INTO projects (name, created) VALUES (?, ?)", (name, utc_now().isoformat())
            )
        return name

    async def projects(self) -> list[str]:
        cur = await self._db.conn.execute("SELECT name FROM projects ORDER BY name")
        return [row["name"] for row in await cur.fetchall()]

    async def submit(
        self,
        client: str,
        project: str,
        model: str,
        task: str,
        priority: int = 0,
        kind: str = protocol.JOB_KIND_AGENT,
    ) -> Job:
        if kind not in protocol.JOB_KINDS:
            raise HubError(
                f"вид задания {kind!r} не поддерживается, есть: {', '.join(protocol.JOB_KINDS)}"
            )
        if not model.strip() or not task.strip():
            raise HubError("у задания должны быть модель и task.md")
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise HubError("приоритет — целое число")
        async with self._tx() as conn:
            cur = await conn.execute("SELECT 1 FROM projects WHERE name = ?", (project,))
            if not await cur.fetchone():
                raise HubError(f"проекта {project!r} нет")
            job_id = await self._free_id(conn)
            await conn.execute(
                "INSERT INTO jobs (id, kind, project, client, model, task, base_branch, priority,"
                " status, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    kind,
                    project,
                    client,
                    model.strip(),
                    task,
                    protocol.BASE_BRANCH_PREFIX + job_id,
                    priority,
                    protocol.STATUS_QUEUED,
                    utc_now().isoformat(),
                ),
            )
            return await self._load(conn, job_id)

    async def get_job(self, job_id: str) -> Job:
        return await self._load(self._db.conn, job_id)

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

    async def claim(self, executor: str, models: Collection[str]) -> Job | None:
        if not models:
            return None
        async with self._tx() as conn:
            cur = await conn.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE status = ? AND kind = ?"
                f" AND model IN ({_in(models)}) ORDER BY priority DESC, seq LIMIT 1",
                (protocol.STATUS_QUEUED, protocol.JOB_KIND_AGENT, *models),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            job = _job(row)
            self._check_transition(job, protocol.STATUS_RUNNING)
            await conn.execute(
                "UPDATE jobs SET status = ?, executor = ?, started = COALESCE(started, ?),"
                " finished = NULL WHERE id = ?",
                (protocol.STATUS_RUNNING, executor, utc_now().isoformat(), job.id),
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
        async with self._tx() as conn:
            job = await self._load(conn, job_id)
            if executor is not None and job.executor != executor:
                raise HubError(f"задание {job_id} ведёт не {executor!r}")
            confirm_stop = kind == protocol.MSG_STOPPED and job.status == protocol.STATUS_STOPPED
            if not confirm_stop:
                target = protocol.MESSAGE_TARGET_STATUS[kind]
                if target is None:
                    if job.status != protocol.STATUS_RUNNING:
                        raise HubError(
                            f"задание {job_id}: {kind} в статусе {job.status} невозможен"
                        )
                else:
                    self._check_transition(job, target)
                    await self._set_status(conn, job, target)
            await conn.execute(
                "UPDATE jobs SET tokens = tokens + ?, cost_usd = cost_usd + ? WHERE id = ?",
                (tokens, cost, job_id),
            )
            return await self._post(conn, job_id, protocol.DIRECTION_TO_CLIENT, kind, payload)

    async def post_from_client(
        self, client: str, job_id: str, kind: str, payload: dict[str, Any] | None = None
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
                if model is not None:
                    await conn.execute(
                        "UPDATE jobs SET model = ? WHERE id = ?", (model.strip(), job_id)
                    )
            elif kind == protocol.MSG_STOP:
                self._check_transition(job, protocol.STATUS_STOPPED)
                await self._set_status(conn, job, protocol.STATUS_STOPPED)
                if job.status == protocol.STATUS_QUEUED:
                    await self._post(
                        conn, job_id, protocol.DIRECTION_TO_CLIENT, protocol.MSG_STOPPED, {}
                    )
            elif job.status != protocol.STATUS_WAITING_OWNER:
                raise HubError(f"release только для задания, ждущего ответа, сейчас {job.status}")
            return await self._post(conn, job_id, protocol.DIRECTION_TO_EXECUTOR, kind, payload)

    async def inbox(self, client: str, since: int = 0, limit: int = DEFAULT_PAGE) -> list[Message]:
        cur = await self._db.conn.execute(
            "SELECT m.* FROM messages m JOIN jobs j ON j.id = m.job_id"
            " WHERE j.client = ? AND m.direction = ? AND m.seq > ? ORDER BY m.seq LIMIT ?",
            (client, protocol.DIRECTION_TO_CLIENT, since, limit),
        )
        return [_message(row) for row in await cur.fetchall()]

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

    async def purge(self, retention_days: int, now: datetime | None = None) -> int:
        border = ((now or utc_now()) - timedelta(days=retention_days)).isoformat()
        async with self._tx() as conn:
            cur = await conn.execute(
                "DELETE FROM messages WHERE read_at IS NOT NULL AND read_at < ?", (border,)
            )
            return cur.rowcount

    @staticmethod
    def _check_transition(job: Job, target: str) -> None:
        if target not in protocol.TRANSITIONS[job.status]:
            raise HubError(f"задание {job.id}: переход {job.status} → {target} недопустим")

    @staticmethod
    async def _set_status(conn: aiosqlite.Connection, job: Job, target: str) -> None:
        finished = utc_now().isoformat() if target in protocol.TERMINAL_STATUSES else None
        await conn.execute(
            "UPDATE jobs SET status = ?, finished = ? WHERE id = ?", (target, finished, job.id)
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
