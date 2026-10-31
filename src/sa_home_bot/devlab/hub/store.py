from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path

from sa_home_bot.db.connection import Database
from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import queue
from sa_home_bot.devlab.hub.errors import HubError
from sa_home_bot.utils.clock import utc_now

HUB_DB_NAME = "hub.sqlite"
TOKEN_BYTES = 32
NAME_MAX_CHARS = 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    name TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    created TEXT NOT NULL,
    revoked TEXT
);
"""


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
        self.queue = queue.HubQueue(self._db)

    async def open(self) -> None:
        await self._db.open()
        await self._db.conn.executescript(SCHEMA + queue.SCHEMA)
        await self._db.conn.commit()

    async def close(self) -> None:
        await self._db.close()

    async def issue_token(self, name: str, role: str) -> str:
        name = name.strip()
        if not name or len(name) > NAME_MAX_CHARS:
            raise HubError(f"имя токена: от 1 до {NAME_MAX_CHARS} символов")
        if role not in protocol.ROLES:
            raise HubError(f"неизвестная роль {role!r}, есть: {', '.join(protocol.ROLES)}")
        token = new_token()
        async with self._db.transaction() as conn:
            cur = await conn.execute("SELECT revoked FROM tokens WHERE name = ?", (name,))
            row = await cur.fetchone()
            if row is not None and row["revoked"] is None:
                raise HubError(f"токен {name!r} уже выпущен: сначала revoke_token")
            await conn.execute("DELETE FROM tokens WHERE name = ?", (name,))
            await conn.execute(
                "INSERT INTO tokens (name, role, hash, created) VALUES (?, ?, ?, ?)",
                (name, role, hash_token(token), utc_now().isoformat()),
            )
        return token

    async def revoke_token(self, name: str) -> None:
        async with self._db.transaction() as conn:
            cur = await conn.execute(
                "UPDATE tokens SET revoked = ? WHERE name = ? AND revoked IS NULL",
                (utc_now().isoformat(), name.strip()),
            )
            if cur.rowcount == 0:
                raise HubError(f"активного токена {name!r} нет")

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
