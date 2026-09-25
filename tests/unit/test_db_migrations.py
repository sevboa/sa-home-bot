"""Точечные миграции db/migrations.py, которые не покрыты фактом простого
"apply_migrations работает" в других тестах — конкретно пересоздание
guest_relationships под CHECK(relation) с 'family' (42.6.5, 2026-09-26): на
живом alfred таблица уже существует со старым CHECK и реальными строками,
пересоздание обязано их сохранить, не потерять."""

from __future__ import annotations

from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations


async def test_guest_relationships_migrates_old_check_and_keeps_rows(tmp_path):
    db = Database(tmp_path / "old.sqlite")
    await db.open()
    # Имитируем состояние alfred ДО 42.6.5: таблица со старым CHECK, с уже
    # существующей подтверждённой связью.
    await db.conn.execute(
        "CREATE TABLE guest_relationships ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "guest_a INTEGER NOT NULL,"
        "guest_b INTEGER NOT NULL,"
        "relation TEXT NOT NULL CHECK (relation IN ('friend', 'acquaintance')),"
        "status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'rejected')),"
        "proposed_by INTEGER NOT NULL,"
        "created_at TEXT NOT NULL,"
        "confirmed_at TEXT)"
    )
    await db.conn.execute(
        "INSERT INTO guest_relationships "
        "(guest_a, guest_b, relation, status, proposed_by, created_at, confirmed_at) "
        "VALUES (301, 302, 'friend', 'confirmed', 301, '2026-09-01T00:00:00+00:00', "
        "'2026-09-01T00:05:00+00:00')"
    )
    await db.conn.commit()

    await apply_migrations(db)

    cur = await db.conn.execute("SELECT * FROM guest_relationships")
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert (rows[0]["guest_a"], rows[0]["guest_b"], rows[0]["relation"], rows[0]["status"]) == (
        301,
        302,
        "friend",
        "confirmed",
    )

    # Новый CHECK реально принимает 'family' теперь.
    await db.conn.execute(
        "INSERT INTO guest_relationships "
        "(guest_a, guest_b, relation, status, proposed_by, created_at) "
        "VALUES (303, 304, 'family', 'pending', 303, '2026-09-26T00:00:00+00:00')"
    )
    await db.conn.commit()
    cur = await db.conn.execute("SELECT COUNT(*) AS n FROM guest_relationships")
    assert (await cur.fetchone())["n"] == 2

    await db.close()


async def test_apply_migrations_idempotent_after_family_added(tmp_path):
    db = Database(tmp_path / "fresh.sqlite")
    await db.open()
    await apply_migrations(db)
    await apply_migrations(db)  # повторный прогон не должен падать/дублировать
    cur = await db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='guest_relationships'"
    )
    row = await cur.fetchone()
    assert "'family'" in row["sql"]
    await db.close()
