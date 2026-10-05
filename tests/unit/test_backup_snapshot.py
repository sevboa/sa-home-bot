"""Динамический снапшот VPN-БД (39.0.8(c)): дамп, seal, доставка напарнику, хранение."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from sa_home_bot.backup import sealed
from sa_home_bot.backup.hold import allow_publish
from sa_home_bot.backup.snapshot import (
    ACTION_GET_SNAPSHOT,
    ACTION_SNAPSHOT_POKE,
    SNAPSHOT_FORMAT,
    SNAPSHOT_TABLES,
    SnapshotError,
    SnapshotReceiver,
    SnapshotSource,
    build_backup,
    dump_tables,
    open_snapshot,
)
from sa_home_bot.backup.store import SNAPSHOT_HISTORY_KEEP, BackupStore
from sa_home_bot.config import BackupConfig, Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.service import VpnService
from tests.unit.test_vpn_service import FakeAwg, allow

PRIV, PUB = sealed.generate_keypair()
CHAT = 111


def _settings(tmp_path, *, release=True, node="jeeves", partner="wooster", pub=True, **backup):
    settings = Settings(
        vpn=VpnConfig(endpoint_host="203.0.113.9", subnet="10.9.0.0/29", base_quota_gb=1),
        backup=BackupConfig(
            recipient_public_key=sealed.dump_key(PUB) if pub else "", partner=partner, **backup
        ),
        node={"id": node, "state_path": str(tmp_path / node / "data" / "node-state.json")},
    )
    if release:
        allow_publish(settings, "test")
    return settings


@pytest_asyncio.fixture
async def db(tmp_path):
    d = Database(tmp_path / "vpn.sqlite")
    await d.open()
    await apply_migrations(d)
    yield d
    await d.close()


async def _fill(db, *, peers=2):
    for i in range(peers):
        await db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, transport, public_key, address, "
            "status, created_at, server) VALUES (?, ?, 'awg', ?, ?, 'active', 't', 'jeeves')",
            (CHAT, f"dev{i}", f"pub{i}", f"10.9.0.{i + 2}"),
        )
    await db.conn.execute(
        "INSERT INTO vpn_chat_access (chat_id, allowed, base_bytes, updated_at) "
        "VALUES (?, 1, NULL, 't')", (CHAT,)
    )
    await db.conn.execute(
        "INSERT INTO vpn_peer_usage (peer_id, month, used_bytes) VALUES (1, '2026-10', 12345)"
    )
    await db.conn.execute(
        "INSERT INTO vpn_quota_grants (chat_id, month, bytes, source, created_at) "
        "VALUES (?, '2026-10', 100, 'self', 't')", (CHAT,)
    )
    await db.conn.commit()


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _source(settings, db, clock=None, notify=None):
    return SnapshotSource(
        settings, lambda: db.conn, "jeeves", notify=notify, clock=clock or Clock()
    )


# --- дамп -> seal -> open ---------------------------------------------------------


async def test_roundtrip_dump_seal_open(db, tmp_path):
    await _fill(db)
    snap = await _source(_settings(tmp_path), db).current()
    doc = open_snapshot(PRIV, snap.blob)
    assert doc["format"] == SNAPSHOT_FORMAT and doc["node"] == "jeeves"
    expect = await dump_tables(db.conn)
    assert doc["tables"] == expect
    peers = doc["tables"]["vpn_peers"]
    assert peers["columns"][:3] == ["id", "chat_id", "device_label"]
    assert [r[peers["columns"].index("public_key")] for r in peers["rows"]] == ["pub0", "pub1"]
    assert doc["tables"]["vpn_peer_usage"]["rows"] == [[1, "2026-10", 12345]]
    # нужные для восстановления таблицы на месте, оперативные — нет
    assert set(doc["tables"]) == set(SNAPSHOT_TABLES)
    assert "vpn_check_states" not in doc["tables"] and "vpn_apk" not in doc["tables"]
    assert snap.meta["rows"]["vpn_peers"] == 2 and snap.meta["rows"]["vpn_peers_active"] == 2


async def test_blob_is_sealed_for_recipient_only(db, tmp_path):
    await _fill(db)
    snap = await _source(_settings(tmp_path), db).current()
    assert b"pub0" not in snap.blob
    other_priv, _ = sealed.generate_keypair()
    with pytest.raises(SnapshotError):
        open_snapshot(other_priv, snap.blob)


async def test_hash_stable_and_rebuild_only_on_change(db, tmp_path):
    await _fill(db)
    clock = Clock()
    src = _source(_settings(tmp_path, snapshot_interval_s=600), db, clock)
    first = await src.current()
    clock.t += 700  # интервал истёк, БД та же
    second = await src.current()
    assert second.meta["hash"] == first.meta["hash"]
    assert second.blob == first.blob and second.meta["rev"] == first.meta["rev"]
    await db.conn.execute("UPDATE vpn_peers SET status = 'revoked' WHERE public_key = 'pub0'")
    await db.conn.commit()
    clock.t += 10  # интервал не истёк — отдаём прежний
    assert (await src.current()).meta["hash"] == first.meta["hash"]
    clock.t += 700
    third = await src.current()
    assert third.meta["hash"] != first.meta["hash"] and third.meta["rev"] == 2
    assert third.meta["rows"]["vpn_peers_active"] == 1


async def test_get_unchanged_and_full(db, tmp_path):
    await _fill(db)
    src = _source(_settings(tmp_path), db)
    full = await src.handle_get({})
    assert full["sealed"] and full["meta"]["hash"] == full["hash"]
    assert await src.handle_get({"have_hash": full["hash"]}) == {
        "unchanged": True, "hash": full["hash"]
    }


# --- приёмник ----------------------------------------------------------------------


def _receiver(settings, source, tmp_path, calls=None):
    store = BackupStore(tmp_path / "wooster" / "backups")

    async def ask(action, args):
        if calls is not None:
            calls.append(action)
        return await source.handle_get(args)

    return SnapshotReceiver(settings, store, ask), store


async def test_receiver_pulls_stores_and_skips_unchanged(db, tmp_path):
    await _fill(db)
    src = _source(_settings(tmp_path), db)
    rcv, store = _receiver(_settings(tmp_path, node="wooster", partner="jeeves"), src, tmp_path)
    assert await rcv.pull_once() is True
    stored = store.load_snapshot("jeeves")
    assert stored.meta["rows"]["vpn_peers"] == 2 and stored.meta["empty"] is False
    assert open_snapshot(PRIV, stored.blob)["tables"]["vpn_peers"]["rows"]
    path = store.snapshots_dir("jeeves") / "latest.sealed"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert await rcv.pull_once() is False  # хеш тот же
    assert store.snapshot_history("jeeves") == []


async def test_receiver_history_is_capped(db, tmp_path):
    await _fill(db)
    clock = Clock()
    src = _source(_settings(tmp_path), db, clock)
    rcv, store = _receiver(_settings(tmp_path, node="wooster", partner="jeeves"), src, tmp_path)
    for i in range(SNAPSHOT_HISTORY_KEEP + 5):
        await db.conn.execute("UPDATE vpn_peer_usage SET used_bytes = ?", (i,))
        await db.conn.commit()
        clock.t += 700
        assert await rcv.pull_once() is True
    hist = store.snapshot_history("jeeves")
    assert len(hist) == SNAPSHOT_HISTORY_KEEP
    assert len(list(hist[0].parent.glob("snapshot.*.meta.json"))) == SNAPSHOT_HISTORY_KEEP


async def test_receiver_rejects_foreign_source(db, tmp_path):
    await _fill(db)
    src = _source(_settings(tmp_path), db)
    # приёмник ждёт «mycraft», а блоб от jeeves
    rcv, store = _receiver(_settings(tmp_path, node="wooster", partner="mycraft"), src, tmp_path)
    with pytest.raises(SnapshotError):
        await rcv.pull_once()
    assert store.load_snapshot("mycraft") is None and store.load_snapshot("jeeves") is None


async def test_empty_snapshot_does_not_displace_last_nonempty(db, tmp_path):
    await _fill(db)
    clock = Clock()
    src = _source(_settings(tmp_path), db, clock)
    rcv, store = _receiver(_settings(tmp_path, node="wooster", partner="jeeves"), src, tmp_path)
    await rcv.pull_once()
    good_hash = store.load_snapshot("jeeves").meta["hash"]
    # «пересобранная нода»: БД пуста
    for table in ("vpn_peers", "vpn_chat_access", "vpn_peer_usage", "vpn_quota_grants"):
        await db.conn.execute(f"DELETE FROM {table}")
    await db.conn.commit()
    # много пустых версий подряд — больше глубины истории
    for i in range(SNAPSHOT_HISTORY_KEEP + 3):
        await db.conn.execute("DELETE FROM vpn_counters")
        await db.conn.execute(
            "INSERT INTO vpn_counters (public_key, last_rx, last_tx) VALUES ('k', ?, 0)", (i,)
        )
        await db.conn.commit()
        clock.t += 700
        await rcv.pull_once()
    latest = store.load_snapshot("jeeves")
    assert latest.meta["empty"] is True  # пустой принимаем — это правда о ноде
    keep = store.load_last_nonempty("jeeves")
    assert keep.meta["hash"] == good_hash and keep.meta["empty"] is False
    assert open_snapshot(PRIV, keep.blob)["tables"]["vpn_peers"]["rows"]


# --- подключение ---------------------------------------------------------------------


async def test_disabled_without_partner_or_key(db, tmp_path):
    async def ask(action, args):  # pragma: no cover
        raise AssertionError

    assert build_backup(_settings(tmp_path, partner=""), lambda: db.conn, "jeeves", ask) is None
    no_key = build_backup(_settings(tmp_path, pub=False), lambda: db.conn, "jeeves", ask)
    assert no_key is not None and no_key.source is None and no_key.receiver is not None
    ids = [s.id for s in no_key.action_specs()]
    # отдавать снапшот нечего — get не объявлен; выдача копий напарника (serve) — есть
    assert ids == [ACTION_SNAPSHOT_POKE, "backup_store_list", "backup_store_get"]
    full = build_backup(_settings(tmp_path), lambda: db.conn, "jeeves", ask)
    assert [s.id for s in full.action_specs()][:2] == [ACTION_GET_SNAPSHOT, ACTION_SNAPSHOT_POKE]


async def test_bad_recipient_key_disables_source(db, tmp_path):
    s = _settings(tmp_path)
    s.backup.recipient_public_key = "not-a-key"

    async def ask(action, args):  # pragma: no cover
        raise AssertionError

    b = build_backup(s, lambda: db.conn, "jeeves", ask)
    assert b.source is None


async def test_poke_wakes_receiver(db, tmp_path):
    await _fill(db)
    src = _source(_settings(tmp_path), db)
    calls: list[str] = []
    settings = _settings(tmp_path, node="wooster", partner="jeeves", snapshot_poll_s=3600)
    rcv, store = _receiver(settings, src, tmp_path, calls)
    from sa_home_bot.backup.snapshot import SnapshotBackup

    backup = SnapshotBackup(None, rcv)
    await backup.start()
    try:
        await asyncio.sleep(0.05)
        assert calls == []  # опрос раз в час — сам не пошёл
        assert await backup.handle(ACTION_SNAPSHOT_POKE, {}) == {"ok": True}
        for _ in range(100):
            if store.load_snapshot("jeeves"):
                break
            await asyncio.sleep(0.02)
        assert store.load_snapshot("jeeves") is not None
    finally:
        await backup.stop()


# --- служба vpn: триггер после изменяющих команд -----------------------------------


@pytest_asyncio.fixture
async def svc(tmp_path, db):
    settings = _settings(tmp_path, snapshot_debounce_s=0.05)
    events: list = []

    async def emit(t, d):
        events.append(t)

    service = VpnService(settings, db, FakeAwg(), emit)
    pokes: list[str] = []

    async def ask(action, args):
        pokes.append(action)
        return {"ok": True}

    service.backup = build_backup(settings, lambda: db.conn, "jeeves", ask)
    yield service, pokes
    await service.backup.stop()


async def test_issue_and_revoke_trigger_snapshot_and_poke(svc, db):
    service, pokes = svc
    await allow(service, CHAT)
    before = await service.backup.source.current()
    await service.run_command(vpn_protocol.ACTION_ISSUE, {"chat_id": CHAT, "device_label": "x"})
    for _ in range(100):
        if pokes:
            break
        await asyncio.sleep(0.02)
    assert pokes == [ACTION_SNAPSHOT_POKE]
    after = await service.backup.source.current()
    assert after.meta["hash"] != before.meta["hash"]
    assert after.meta["rows"]["vpn_peers_active"] == 1
    label = open_snapshot(PRIV, after.blob)["tables"]["vpn_peers"]["rows"][0][2]
    pokes.clear()
    await service.run_command(
        vpn_protocol.ACTION_REVOKE, {"chat_id": CHAT, "device_label": label}
    )
    for _ in range(100):
        if pokes:
            break
        await asyncio.sleep(0.02)
    assert pokes == [ACTION_SNAPSHOT_POKE]
    assert (await service.backup.source.current()).meta["rows"]["vpn_peers_active"] == 0


async def test_read_commands_do_not_trigger(svc):
    service, pokes = svc
    await service.run_command(vpn_protocol.ACTION_PEERS, {})
    await asyncio.sleep(0.15)
    assert pokes == []


async def test_snapshot_actions_are_described_and_served(svc):
    service, _ = svc
    ids = {a.id for a in service.describe().actions}
    assert {ACTION_GET_SNAPSHOT, ACTION_SNAPSHOT_POKE} <= ids
    resp = await service.run_command(ACTION_GET_SNAPSHOT, {})
    assert resp["sealed"] and resp["meta"]["source"] == "jeeves"
