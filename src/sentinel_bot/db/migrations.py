"""Идемпотентное применение схемы из schema.sql."""

from __future__ import annotations

import logging
from importlib import resources

from sentinel_bot.db.connection import Database

log = logging.getLogger(__name__)


def _load_schema() -> str:
    return resources.files("sentinel_bot.db").joinpath("schema.sql").read_text(encoding="utf-8")


async def apply_migrations(db: Database) -> None:
    schema = _load_schema()
    await db.conn.executescript(schema)
    await db.conn.commit()
    log.info("Схема БД применена")
