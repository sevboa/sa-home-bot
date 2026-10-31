from __future__ import annotations

from pathlib import Path

from sa_home_bot.db.connection import Database
from sa_home_bot.utils.clock import utc_now

LEDGER_DB_NAME = "ledger.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    day TEXT NOT NULL,
    model TEXT NOT NULL,
    job TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model, job)
);
"""


def today() -> str:
    return utc_now().date().isoformat()


class Ledger:
    def __init__(self, data_dir: Path) -> None:
        self._db = Database(data_dir / LEDGER_DB_NAME)

    async def open(self) -> None:
        await self._db.open()
        await self._db.conn.executescript(SCHEMA)
        await self._db.conn.commit()

    async def close(self) -> None:
        await self._db.close()

    async def requests_today(self, model: str) -> int:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) AS n FROM usage WHERE day = ? AND model = ?",
            (today(), model),
        )
        row = await cur.fetchone()
        return row["n"] if row else 0

    async def add(
        self,
        model: str,
        job: str,
        *,
        requests: int = 0,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO usage (day, model, job, requests, tokens_in, tokens_out, cost_usd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(day, model, job) DO UPDATE SET"
                " requests = requests + excluded.requests,"
                " tokens_in = tokens_in + excluded.tokens_in,"
                " tokens_out = tokens_out + excluded.tokens_out,"
                " cost_usd = cost_usd + excluded.cost_usd",
                (today(), model, job, requests, tokens_in, tokens_out, cost_usd),
            )

    async def totals(self, model: str, job: str) -> dict[str, float]:
        cur = await self._db.conn.execute(
            "SELECT COALESCE(SUM(requests), 0) AS requests,"
            " COALESCE(SUM(tokens_in), 0) AS tokens_in,"
            " COALESCE(SUM(tokens_out), 0) AS tokens_out,"
            " COALESCE(SUM(cost_usd), 0) AS cost_usd"
            " FROM usage WHERE day = ? AND model = ? AND job = ?",
            (today(), model, job),
        )
        row = await cur.fetchone()
        return dict(row) if row else {}
