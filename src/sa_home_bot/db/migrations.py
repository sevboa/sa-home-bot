"""Идемпотентное применение схемы из schema.sql.

CREATE TABLE IF NOT EXISTS в schema.sql не подхватывает новые колонки на уже
существующей таблице (БД в проде не пересоздаётся) — такие точечные
довески оформляются здесь через ALTER TABLE с проверкой наличия колонки.
"""

from __future__ import annotations

import logging
from importlib import resources

from sa_home_bot.db.connection import Database

log = logging.getLogger(__name__)


def _load_schema() -> str:
    return resources.files("sa_home_bot.db").joinpath("schema.sql").read_text(encoding="utf-8")


async def _add_column_if_missing(db: Database, table: str, column: str, decl: str) -> None:
    cur = await db.conn.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in await cur.fetchall()}
    if column not in existing:
        await db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        log.info("Миграция: %s.%s добавлена", table, column)


async def apply_migrations(db: Database) -> None:
    schema = _load_schema()
    await db.conn.executescript(schema)
    # ai_turns.user_id/user_name — добавлены 2026-07-24 (см. schema.sql).
    await _add_column_if_missing(db, "ai_turns", "user_id", "INTEGER")
    await _add_column_if_missing(db, "ai_turns", "user_name", "TEXT")
    await db.conn.commit()
    log.info("Схема БД применена")
