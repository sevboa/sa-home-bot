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
    # ai_turns.photo_path — добавлена 2026-08-10, мультимодальный /ai (см. schema.sql).
    await _add_column_if_missing(db, "ai_turns", "photo_path", "TEXT")
    # vpn_peers.server — добавлена 2026-09-05, два VPN-сервера (этап 39). Бэкфилл
    # NULL → имя своей ноды делает vpn/service.py::backfill_server (там есть
    # [node].id; в общей миграции хардкодить "jeeves" не хочется).
    await _add_column_if_missing(db, "vpn_peers", "server", "TEXT")
    # vpn_peers.transport — добавлена 2026-09-10, второй транспорт службы vpn
    # (VLESS+Reality). DEFAULT 'awg' верно бэкфиллит все существующие пиры
    # (до этого транспорт был только один — AmneziaWG). См. schema.sql.
    await _add_column_if_missing(db, "vpn_peers", "transport", "TEXT NOT NULL DEFAULT 'awg'")
    # vpn_check_states: PK (node, target) → (node, server, transport, target) —
    # этап 39.0.7, 2026-09-18 (несколько VPN-серверов и транспортов). Старую
    # форму таблицы (без server/transport) ALTER TABLE не спасает — PK не
    # поменять, а бэкфиллить некуда (в старых строках сервер/транспорт не
    # записаны, а данные проверки — оперативные, не жалко). Пересоздаём: если
    # таблица есть и колонки server ещё нет — снести и дать schema.sql выше
    # создать заново в новой форме.
    cur = await db.conn.execute("PRAGMA table_info(vpn_check_states)")
    existing = {row["name"] for row in await cur.fetchall()}
    if existing and "server" not in existing:
        await db.conn.execute("DROP TABLE vpn_check_states")
        await db.conn.executescript(schema)
        log.info(
            "Миграция: vpn_check_states пересоздана под ключ (node, server, transport, target)"
        )
    await db.conn.commit()
    log.info("Схема БД применена")
