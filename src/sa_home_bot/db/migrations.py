from __future__ import annotations

import logging
from importlib import resources

from sa_home_bot.db.connection import Database

log = logging.getLogger(__name__)


def _load_schema() -> str:
    return resources.files("sa_home_bot.db").joinpath("schema.sql").read_text(encoding="utf-8")


async def _add_column_if_missing(db: Database, table: str, column: str, decl: str) -> bool:
    cur = await db.conn.execute(f"PRAGMA table_info({table})")
    existing = {row["name"] for row in await cur.fetchall()}
    if column in existing:
        return False
    await db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    log.info("Миграция: %s.%s добавлена", table, column)
    return True


async def apply_migrations(db: Database) -> None:
    schema = _load_schema()
    await db.conn.executescript(schema)
    await _add_column_if_missing(db, "ai_turns", "user_id", "INTEGER")
    await _add_column_if_missing(db, "ai_turns", "user_name", "TEXT")
    await _add_column_if_missing(db, "ai_turns", "photo_path", "TEXT")
    await _add_column_if_missing(db, "vpn_peers", "server", "TEXT")
    await _add_column_if_missing(db, "vpn_peers", "transport", "TEXT NOT NULL DEFAULT 'awg'")
    await _add_column_if_missing(db, "vpn_peers", "server_pubkey", "TEXT")
    cur = await db.conn.execute("PRAGMA table_info(vpn_check_states)")
    existing = {row["name"] for row in await cur.fetchall()}
    if existing and "server" not in existing:
        await db.conn.execute("DROP TABLE vpn_check_states")
        await db.conn.executescript(schema)
        log.info(
            "Миграция: vpn_check_states пересоздана под ключ (node, server, transport, target)"
        )
    if await _add_column_if_missing(db, "pending_actions", "welcome_message_id", "INTEGER"):
        await db.conn.execute(
            "UPDATE pending_actions SET welcome_message_id=0 WHERE status='accepted'"
        )
    await _migrate_guest_relationships_check(db, schema)
    await _add_column_if_missing(db, "images", "purpose", "TEXT NOT NULL DEFAULT 'chat'")
    await _add_column_if_missing(db, "images", "params", "TEXT")
    await _backfill_ai_turns_fts(db)
    await db.conn.commit()
    log.info("Схема БД применена")


_LEGACY_RELATION_TYPES = ("friend", "family", "spouse")


async def _migrate_guest_relationships_check(db: Database, schema: str) -> None:
    cur = await db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='guest_relationships'"
    )
    row = await cur.fetchone()
    if row is None or not any(f"'{r}'" in row["sql"] for r in _LEGACY_RELATION_TYPES):
        return
    await db.conn.execute("ALTER TABLE guest_relationships RENAME TO guest_relationships_old_46")
    await db.conn.executescript(schema)
    await db.conn.execute(
        "INSERT INTO guest_relationships "
        "(id, guest_a, guest_b, relation, status, proposed_by, created_at, confirmed_at) "
        "SELECT id, guest_a, guest_b, 'acquaintance', status, proposed_by, created_at, "
        "confirmed_at FROM guest_relationships_old_46"
    )
    await db.conn.execute("DROP TABLE guest_relationships_old_46")
    log.info("Миграция: guest_relationships — все связи переведены в знакомство")


async def _backfill_ai_turns_fts(db: Database) -> None:
    cur = await db.conn.execute("SELECT 1 FROM ai_turns_fts LIMIT 1")
    if await cur.fetchone() is not None:
        return
    cur = await db.conn.execute(
        "INSERT INTO ai_turns_fts(content, chat_id, dialogue_id, message_id) "
        "SELECT replace(replace(content, 'ё', 'е'), 'Ё', 'Е'), chat_id, dialogue_id, "
        "message_id FROM ai_turns"
    )
    if cur.rowcount:
        log.info("Миграция: ai_turns_fts — проиндексировано ходов /ai: %d", cur.rowcount)
