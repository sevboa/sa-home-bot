from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sa_home_bot.db.connection import Database
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import queue, repos
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.utils.clock import utc_now

HUB_DB_NAME = "hub.sqlite"
GIT_DIR_NAME = "git"
LOGS_DIR_NAME = "logs"
TOKEN_BYTES = 32
NAME_MAX_CHARS = 64
HEARTBEAT_FRESH_S = 120

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    revoked TEXT,
    PRIMARY KEY (name, role)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    data TEXT NOT NULL,
    seen TEXT NOT NULL,
    PRIMARY KEY (name, role)
);
"""

KEYED_TABLES = {
    "tokens": "name, role, hash, created, revoked",
    "heartbeats": "name, role, data, seen",
}


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return protocol.TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


class HubStore:
    def __init__(self, data_dir: Path) -> None:
        self._db = Database(data_dir / HUB_DB_NAME)
        self.git_root = data_dir / GIT_DIR_NAME
        self.logs_root = data_dir / LOGS_DIR_NAME
        self.queue = queue.HubQueue(self._db, self.logs_root)

    async def open(self) -> None:
        await self._db.open()
        legacy = [t for t in KEYED_TABLES if await self._keyed_by_name_only(t)]
        for table in legacy:
            await self._db.conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
        await self._db.conn.executescript(SCHEMA + queue.SCHEMA)
        for table in legacy:
            columns = KEYED_TABLES[table]
            await self._db.conn.execute(
                f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_legacy"
            )
            await self._db.conn.execute(f"DROP TABLE {table}_legacy")
        if not await self._has_column("jobs", queue.PARAMS_COLUMN):
            await self._db.conn.execute(queue.ADD_PARAMS_COLUMN)
        await self._db.conn.commit()

    async def _has_column(self, table: str, column: str) -> bool:
        cur = await self._db.conn.execute(f"PRAGMA table_info({table})")
        return any(row["name"] == column for row in await cur.fetchall())

    async def _keyed_by_name_only(self, table: str) -> bool:
        cur = await self._db.conn.execute(f"PRAGMA table_info({table})")
        columns = await cur.fetchall()
        key = {row["name"] for row in columns if row["pk"]}
        return bool(columns) and key == {"name"}

    async def close(self) -> None:
        await self._db.close()

    async def add_project(self, name: str) -> Path:
        name = await self.queue.add_project(name)
        try:
            return await repos.init_bare(self.git_root, name)
        except (HubError, OSError):
            await self.queue.remove_project(name)
            raise

    async def issue_token(self, name: str, role: str, replace: bool = False) -> str:
        name = name.strip()
        if not name or len(name) > NAME_MAX_CHARS:
            raise HubError(f"имя токена: от 1 до {NAME_MAX_CHARS} символов")
        if role not in protocol.ROLES:
            raise HubError(f"неизвестная роль {role!r}, есть: {', '.join(protocol.ROLES)}")
        token = new_token()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "SELECT revoked FROM tokens WHERE name = ? AND role = ?", (name, role)
            )
            row = await cur.fetchone()
            if row is not None and row["revoked"] is None and not replace:
                raise HubError(f"токен {name!r} уже выпущен: сначала revoke_token")
            await conn.execute("DELETE FROM tokens WHERE name = ? AND role = ?", (name, role))
            await conn.execute(
                "INSERT INTO tokens (name, role, hash, created) VALUES (?, ?, ?, ?)",
                (name, role, hash_token(token), utc_now().isoformat()),
            )
        return token

    async def revoke_token(self, name: str, role: str = "") -> None:
        name = name.strip()
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "SELECT role FROM tokens WHERE name = ? AND revoked IS NULL", (name,)
            )
            active = [row["role"] for row in await cur.fetchall()]
            if not active:
                raise HubError(f"активного токена {name!r} нет")
            if not role and len(active) > 1:
                raise HubError(
                    f"у {name!r} активны роли {', '.join(sorted(active))}: укажи role"
                )
            target = role or active[0]
            if target not in active:
                raise HubError(f"активного токена {name!r} роли {target!r} нет")
            await conn.execute(
                "UPDATE tokens SET revoked = ? WHERE name = ? AND role = ?",
                (utc_now().isoformat(), name, target),
            )

    async def authenticate(self, token: str) -> Principal | None:
        cur = await self._db.conn.execute(
            "SELECT name, role FROM tokens WHERE hash = ? AND revoked IS NULL",
            (hash_token(token),),
        )
        row = await cur.fetchone()
        return Principal(row["name"], row["role"]) if row else None

    async def count_active(self) -> int:
        cur = await self._db.conn.execute("SELECT count(*) AS n FROM tokens WHERE revoked IS NULL")
        row = await cur.fetchone()
        return row["n"] if row else 0

    async def heartbeat(self, principal: Principal, data: dict[str, Any]) -> None:
        if principal.role == protocol.ROLE_GATEWAY:
            models = data.get(protocol.FIELD_MODELS)
            if not isinstance(models, list) or not all(
                isinstance(m, dict) and m.get(protocol.MODEL_NAME) for m in models
            ):
                raise HubError("heartbeat шлюза: models — список объектов с непустым name")
            if not isinstance(data.get(protocol.FIELD_URL, ""), str):
                raise HubError("heartbeat шлюза: url — строка")
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO heartbeats (name, role, data, seen) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name, role) DO UPDATE SET data = excluded.data, seen = excluded.seen",
                (
                    principal.name,
                    principal.role,
                    json.dumps(data, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )

    async def executor_alive(self) -> bool:
        border = (utc_now() - timedelta(seconds=HEARTBEAT_FRESH_S)).isoformat()
        cur = await self._db.conn.execute(
            "SELECT 1 FROM heartbeats WHERE role = ? AND seen >= ?",
            (protocol.ROLE_EXECUTOR, border),
        )
        return await cur.fetchone() is not None

    async def models(self, with_urls: bool = False) -> list[dict[str, Any]]:
        border = (utc_now() - timedelta(seconds=HEARTBEAT_FRESH_S)).isoformat()
        cur = await self._db.conn.execute(
            "SELECT name, data FROM heartbeats WHERE role = ? AND seen >= ? ORDER BY name",
            (protocol.ROLE_GATEWAY, border),
        )
        catalog: list[dict[str, Any]] = []
        for row in await cur.fetchall():
            data = json.loads(row["data"])
            limits = data.get(protocol.FIELD_LIMITS)
            url = {protocol.MODEL_URL: data.get(protocol.FIELD_URL, "")} if with_urls else {}
            catalog.extend(
                {
                    **model,
                    protocol.MODEL_GATEWAY: row["name"],
                    protocol.FIELD_LIMITS: limits,
                    **url,
                }
                for model in data[protocol.FIELD_MODELS]
            )
        return catalog
