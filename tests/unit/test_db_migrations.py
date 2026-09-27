"""Точечные миграции db/migrations.py, которые не покрыты фактом простого
"apply_migrations работает" в других тестах: пересоздание
guest_relationships под CHECK только на 'acquaintance' (Этап 46,
2026-09-27 — связь между гостями одна, знакомство) и колонка
pending_actions.welcome_message_id. На живом alfred таблица уже существует
со старым CHECK и реальными строками — пересоздание обязано их сохранить,
переписав тип в знакомство."""

from __future__ import annotations

import pytest

from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.db.store import Store

_OLD_TABLE = (
    "CREATE TABLE guest_relationships ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "guest_a INTEGER NOT NULL,"
    "guest_b INTEGER NOT NULL,"
    "relation TEXT NOT NULL CHECK (relation IN ({check})),"
    "status TEXT NOT NULL CHECK (status IN ('pending', 'confirmed', 'rejected')),"
    "proposed_by INTEGER NOT NULL,"
    "created_at TEXT NOT NULL,"
    "confirmed_at TEXT)"
)


@pytest.mark.parametrize(
    "check",
    [
        "'friend', 'acquaintance'",  # 42.6.1
        "'friend', 'acquaintance', 'family'",  # 42.6.5, alfred v0.112.13
        "'friend', 'acquaintance', 'family', 'spouse'",  # 42.6.7, alfred v0.113.0
    ],
)
async def test_guest_relationships_old_types_become_acquaintance(tmp_path, check):
    db = Database(tmp_path / "old.sqlite")
    await db.open()
    await db.conn.execute(_OLD_TABLE.format(check=check))
    await db.conn.execute(
        "INSERT INTO guest_relationships "
        "(guest_a, guest_b, relation, status, proposed_by, created_at, confirmed_at) "
        "VALUES (301, 302, 'friend', 'confirmed', 301, '2026-09-01T00:00:00+00:00', "
        "'2026-09-01T00:05:00+00:00')"
    )
    await db.conn.execute(
        "INSERT INTO guest_relationships "
        "(guest_a, guest_b, relation, status, proposed_by, created_at) "
        "VALUES (303, 304, 'acquaintance', 'rejected', 303, '2026-09-02T00:00:00+00:00')"
    )
    await db.conn.commit()

    await apply_migrations(db)

    cur = await db.conn.execute("SELECT * FROM guest_relationships ORDER BY id")
    rows = [dict(r) for r in await cur.fetchall()]
    assert [(r["guest_a"], r["guest_b"], r["relation"], r["status"]) for r in rows] == [
        (301, 302, "acquaintance", "confirmed"),
        (303, 304, "acquaintance", "rejected"),
    ]
    assert rows[0]["confirmed_at"] == "2026-09-01T00:05:00+00:00"

    # Новый CHECK старые типы больше не принимает.
    with pytest.raises(Exception, match="CHECK"):
        await db.conn.execute(
            "INSERT INTO guest_relationships "
            "(guest_a, guest_b, relation, status, proposed_by, created_at) "
            "VALUES (305, 306, 'spouse', 'pending', 305, '2026-09-26T00:00:00+00:00')"
        )
    await db.close()


async def test_pending_actions_gets_welcome_message_id(tmp_path):
    """Таблица pending_actions v0.113.0 — без welcome_message_id."""
    db = Database(tmp_path / "v113.sqlite")
    await db.open()
    await db.conn.execute(
        "CREATE TABLE pending_actions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, "
        "initiator INTEGER NOT NULL, addressee INTEGER, payload_json TEXT NOT NULL, "
        "status TEXT NOT NULL, expires_at TEXT NOT NULL, draft_message_id INTEGER, "
        "offer_message_id INTEGER, notice_message_id INTEGER, created_at TEXT NOT NULL, "
        "submitted_at TEXT, decided_at TEXT, decided_by INTEGER, reason TEXT)"
    )
    await db.conn.execute(
        "INSERT INTO pending_actions(kind, initiator, addressee, payload_json, status, "
        "expires_at, notice_message_id, created_at) VALUES ('relationship', 1, 2, '{}', "
        "'accepted', '2026-09-27T12:00:00+00:00', 18724, '2026-09-27T11:00:00+00:00')"
    )
    await db.conn.commit()

    await apply_migrations(db)

    cur = await db.conn.execute("PRAGMA table_info(pending_actions)")
    assert "welcome_message_id" in {r["name"] for r in await cur.fetchall()}
    # Принятая до Этапа 46 форма поздравления не ждёт — recover() её не тронет.
    store = Store(db)
    assert await store.pending_actions_needing_delivery() == []
    await db.close()


async def test_apply_migrations_idempotent_acquaintance_only(tmp_path):
    db = Database(tmp_path / "fresh.sqlite")
    await db.open()
    await apply_migrations(db)
    await apply_migrations(db)  # повторный прогон не должен падать/дублировать
    cur = await db.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='guest_relationships'"
    )
    row = await cur.fetchone()
    assert "'acquaintance'" in row["sql"]
    for legacy in ("'friend'", "'family'", "'spouse'"):
        assert legacy not in row["sql"]
    await db.close()
