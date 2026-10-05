"""Восстановление vpn-сервера (39.0.8(d)): отдача копий, list/dry-run, выбор снапшота,
применение на ноде, защита от затирания, полный round-trip jeeves -> wooster -> restore."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
import pytest_asyncio

from sa_home_bot.backup import apply as bapply
from sa_home_bot.backup import restore as rs
from sa_home_bot.backup import sealed, serve
from sa_home_bot.backup.hold import allow_publish, publish_allowed
from sa_home_bot.backup.identity import IdentityPublisher, parse_package
from sa_home_bot.backup.snapshot import SnapshotSource, build_backup
from sa_home_bot.backup.store import BackupStore
from sa_home_bot.config import BackupConfig, RealityTransportConfig, Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.node.instances import InstanceMeta, InstanceStore
from sa_home_bot.proto.messages import ProtoError

PRIV, PUB = sealed.generate_keypair()
AWG_PRIV, _ = sealed.generate_keypair()
AWG_KEY = sealed.dump_key(AWG_PRIV)  # формат ключа WireGuard тот же: base64 от 32 байт
XRAY = {
    "inbounds": [
        {"tag": "api-in", "protocol": "dokodemo-door"},
        {
            "tag": "reality-in",
            "settings": {"clients": [{"id": "u1"}]},
            "streamSettings": {"realitySettings": {"privateKey": "REALPRIV", "shortIds": ["ab12"]}},
        },
    ]
}
CHAT = 111


def _settings(tmp_path, node, partner, *, jc=7, release=True):
    s = Settings(
        vpn=VpnConfig(
            endpoint_host="203.0.113.9", jc=jc, h1=1000001, transports=["awg", "reality"],
            db_path=tmp_path / node / "vpn.sqlite",
            reality=RealityTransportConfig(server_public_key="REALPUB", short_id="ab12"),
        ),
        backup=BackupConfig(
            recipient_public_key=sealed.dump_key(PUB), partner=partner,
            xray_config=str(tmp_path / node / "xray.json"),
        ),
        node={"id": node, "state_path": str(tmp_path / node / "data" / "node-state.json")},
    )
    if release:
        allow_publish(s, "test")
    return s


async def _fill(db, peers=2):
    for i in range(peers):
        await db.conn.execute(
            "INSERT INTO vpn_peers (id, chat_id, device_label, transport, public_key, address, "
            "status, created_at, server) VALUES (?, ?, ?, 'awg', ?, ?, 'active', 't', 'jeeves')",
            (10 + i, CHAT, f"dev{i}", f"pub{i}", f"10.9.0.{i + 2}"),
        )
    await db.conn.execute(
        "INSERT INTO vpn_chat_access (chat_id, allowed, updated_at) VALUES (?, 1, 't')", (CHAT,)
    )
    await db.conn.execute(
        "INSERT INTO vpn_peer_usage (peer_id, month, used_bytes) VALUES (10, '2026-10', 777)"
    )
    await db.conn.execute(
        "INSERT INTO vpn_counters (public_key, last_rx, last_tx) VALUES ('pub0', 5, 6)"
    )
    await db.conn.commit()


@pytest_asyncio.fixture
async def world(tmp_path):
    """jeeves (источник) -> хранилище wooster. Возвращает всё нужное тестам."""
    jeeves = _settings(tmp_path, "jeeves", "wooster")
    wooster = _settings(tmp_path, "wooster", "jeeves")
    db = Database(jeeves.vpn.db_path)
    await db.open()
    await apply_migrations(db)
    await _fill(db)

    # identity: собираем как публикатор jeeves, кладём в хранилище wooster
    class Awg:
        async def __call__(self):
            return AWG_KEY

    inst = InstanceStore(tmp_path / "jeeves" / "instances", "jeeves")
    pub = IdentityPublisher(
        jeeves, inst, "jeeves", read_awg_private_key=Awg(), read_xray=lambda: XRAY
    )
    assert await pub.publish_if_changed()
    pkg = inst.package_path("vpn-identity", "jeeves").read_bytes()
    source, blob = parse_package(pkg)
    store = BackupStore(tmp_path / "wooster" / "data" / "backups")
    store.save_identity(
        source, blob, InstanceMeta("vpn-identity", "jeeves", 1, "h", "2026-10-05T10:00:00+00:00",
                                   "jeeves")
    )
    src = SnapshotSource(jeeves, lambda: db.conn, "jeeves")
    snap = await src.current()
    store.save_snapshot("jeeves", snap.blob, snap.meta)
    backup = build_backup(wooster, lambda: None, "wooster", _unused)
    backup.store = store

    async def ask(action, args):
        res = await backup.handle(action, args)
        if res is None:
            raise AssertionError(action)
        return res

    yield {"store": store, "ask": ask, "db": db, "jeeves": jeeves, "wooster": wooster,
           "tmp": tmp_path, "src": src}
    await db.close()


async def _unused(action, args):  # pragma: no cover
    raise AssertionError


# --- отдача копий -----------------------------------------------------------------


async def test_serve_list_and_get(world):
    ask = world["ask"]
    listing = await ask(serve.ACTION_STORE_LIST, {"node": "jeeves"})
    assert [v["label"] for v in listing["identity"]] == ["latest"]
    labels = [v["label"] for v in listing["snapshot"]]
    assert labels == ["latest", "last_nonempty"]
    assert listing["snapshot"][0]["meta"]["rows"]["vpn_peers"] == 2
    got = await ask(serve.ACTION_STORE_GET, {"node": "jeeves", "kind": "identity"})
    assert got["label"] == "latest" and got["sealed"]
    # блоб запечатан: открытого ключа сервера в нём нет
    assert AWG_KEY.encode() not in got["sealed"].encode()


async def test_serve_refuses_foreign_node_and_unknown(world):
    ask = world["ask"]
    with pytest.raises(ProtoError):
        await ask(serve.ACTION_STORE_LIST, {"node": "mycraft"})
    with pytest.raises(ProtoError):
        await ask(serve.ACTION_STORE_GET, {"node": "jeeves", "kind": "snapshot", "label": "../x"})
    with pytest.raises(ProtoError):
        await ask(serve.ACTION_STORE_GET, {"node": "jeeves", "kind": "secrets"})


async def test_serve_history_labels(world):
    store = world["store"]
    meta = InstanceMeta("vpn-identity", "jeeves", 2, "h2", "2026-10-06T10:00:00+00:00", "jeeves")
    store.save_identity("jeeves", b"other-blob", meta)  # прежняя уйдёт в history
    listing = await world["ask"](serve.ACTION_STORE_LIST, {"node": "jeeves"})
    labels = [v["label"] for v in listing["identity"]]
    assert labels[0] == "latest" and len(labels) == 2
    old = await world["ask"](
        serve.ACTION_STORE_GET, {"node": "jeeves", "kind": "identity", "label": labels[1]}
    )
    assert old["sealed"]


# --- выбор снапшота -------------------------------------------------------------------


def _v(label, peers, empty=False):
    return {"label": label, "meta": {"rows": {"vpn_peers": peers}, "empty": empty}}


def test_pick_snapshot_default_latest():
    assert rs.pick_snapshot([_v("latest", 3), _v("last_nonempty", 3)], None) == "latest"
    assert rs.pick_snapshot([], None) is None


def test_pick_snapshot_empty_latest_refused_with_hint():
    vs = [_v("latest", 0, True), _v("last_nonempty", 5)]
    with pytest.raises(rs.RestoreError, match="last_nonempty"):
        rs.pick_snapshot(vs, None)
    assert rs.pick_snapshot(vs, "last_nonempty") == "last_nonempty"
    assert rs.pick_snapshot(vs, "latest") == "latest"
    with pytest.raises(rs.RestoreError):
        rs.pick_snapshot(vs, "nope")
    # пустой latest, а ненпустого нет — восстанавливать нечего, но это не отказ
    assert rs.pick_snapshot([_v("latest", 0, True)], None) == "latest"


# --- fetch / dry-run -------------------------------------------------------------------


async def test_fetch_bundle_matches_source(world):
    bundle = await rs.fetch_bundle(world["ask"], "jeeves", PRIV)
    assert bundle["identity"]["awg"]["private_key"] == AWG_KEY
    assert bundle["identity"]["awg"]["obfuscation"]["jc"] == 7
    assert bundle["identity"]["reality"]["private_key"] == "REALPRIV"
    assert bundle["snapshot_label"] == "latest"
    from sa_home_bot.backup.snapshot import dump_tables

    assert bundle["snapshot"]["tables"] == await dump_tables(world["db"].conn)


async def test_fetch_wrong_key_fails(world):
    other, _ = sealed.generate_keypair()
    with pytest.raises(rs.RestoreError):
        await rs.fetch_bundle(world["ask"], "jeeves", other)


async def test_dry_run_dir_permissions_and_content(world, tmp_path):
    bundle = await rs.fetch_bundle(world["ask"], "jeeves", PRIV)
    d = tmp_path / "check"
    rs.write_dir(d, bundle)
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    for f in d.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert json.loads((d / "identity.json").read_text()) == bundle["identity"]
    assert json.loads((d / "snapshot.json").read_text()) == bundle["snapshot"]
    summary = (d / "summary.txt").read_text()
    assert "пиров 2" in summary and sealed.dump_key(
        sealed.public_from_private(AWG_PRIV)
    ) in summary
    assert AWG_KEY not in summary and "REALPRIV" not in summary  # секретов в сводке нет
    # каталог можно прочитать обратно как бандл
    assert rs.load_bundle(d)["identity"] == bundle["identity"]
    with pytest.raises(rs.RestoreError):  # непустой каталог не затираем
        rs.write_dir(d, bundle)


# --- применение -----------------------------------------------------------------------


class FakeIO:
    def __init__(self, files=None, active=()):
        self.files = dict(files or {})
        self.active = set(active)
        self.restarted = []

    def read_root(self, path):
        return self.files.get(Path(path))

    def write_root(self, path, text):
        self.files[Path(path)] = text

    def unit_active(self, unit, *, user=False):
        return unit in self.active

    def restart(self, unit, *, user=False):
        self.restarted.append(unit)


def _target(tmp_path, node="jeeves"):
    """Чистая пересобранная нода: свой конфиг, БД нет, awg0.conf со СВЕЖИМИ ключами."""
    s = _settings(tmp_path / "new", node, "wooster", release=False)
    s.vpn.jc = 99  # свежесгенерированная обфускация — должна быть заменена
    cfg = tmp_path / "new" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        '[node]\nid = "jeeves"\n\n[vpn]\nsocket = "/abs/vpn.sock"\njc = 99\n'
        'endpoint_host = "203.0.113.9"\n\n[vpn.reality]\nport = 1\n\n'
        '[backup]\npartner = "wooster"\n'
    )
    return s, cfg


async def test_apply_identity_and_rows(world, tmp_path):
    bundle = await rs.fetch_bundle(world["ask"], "jeeves", PRIV)
    s, cfg = _target(tmp_path)
    awg = tmp_path / "awg0.conf"
    env = tmp_path / "reality.env"
    io = FakeIO(
        {awg: "[Interface]\nAddress = 10.9.0.1/24\nPrivateKey = FRESH\nJc = 99\nS1 = 3\n"
              "[Peer]\nPublicKey = x\n"},
        active={"awg-quick@awg0", "xray.service"},
    )
    xray = Path(s.backup.xray_config)
    xray.parent.mkdir(parents=True, exist_ok=True)
    fresh = json.loads(json.dumps(XRAY))
    fresh["inbounds"][1]["streamSettings"]["realitySettings"].update(
        privateKey="FRESH", shortIds=["zz"]
    )
    xray.write_text(json.dumps(fresh))
    logs = []
    await bapply.apply_bundle(bundle, s, config_path=cfg, io=io, awg_conf=awg,
                              reality_env=env, log=logs.append)
    conf = io.files[awg]
    assert f"PrivateKey = {AWG_KEY}" in conf and "FRESH" not in conf
    assert "Jc = 7" in conf and "H1 = 1000001" in conf
    assert "Address = 10.9.0.1/24" in conf and "[Peer]\nPublicKey = x" in conf  # прочее цело
    assert conf.count("PrivateKey") == 1 and conf.count("Jc") == 1
    assert "REALITY_PRIVATE_KEY=REALPRIV" in io.files[env]
    assert "REALITY_PUBLIC_KEY=REALPUB" in io.files[env] and "SHORT_ID=ab12" in io.files[env]
    doc = json.loads(xray.read_text())
    rs_ = doc["inbounds"][1]["streamSettings"]["realitySettings"]
    assert rs_["privateKey"] == "REALPRIV" and rs_["shortIds"] == ["ab12"]
    assert doc["inbounds"][1]["settings"]["clients"] == [{"id": "u1"}]
    import tomllib

    toml = tomllib.loads(cfg.read_text())
    assert toml["vpn"]["jc"] == 7 and toml["vpn"]["socket"] == "/abs/vpn.sock"
    assert toml["vpn"]["transports"] == ["awg", "reality"]
    assert toml["vpn"]["reality"]["server_public_key"] == "REALPUB"
    assert toml["node"]["id"] == "jeeves" and toml["backup"]["partner"] == "wooster"
    assert (cfg.parent / "config.toml.pre-restore").exists()
    assert io.restarted == ["awg-quick@awg0", "xray.service"]
    assert publish_allowed(s)  # публикация снова разрешена
    # строки: id сохранён, counters не перенесены, usage ссылается на тот же peer_id
    db = Database(s.vpn.db_path)
    await db.open()
    try:
        cur = await db.conn.execute("SELECT id, public_key FROM vpn_peers ORDER BY id")
        assert [tuple(r) for r in await cur.fetchall()] == [(10, "pub0"), (11, "pub1")]
        cur = await db.conn.execute("SELECT peer_id, used_bytes FROM vpn_peer_usage")
        assert [tuple(r) for r in await cur.fetchall()] == [(10, 777)]
        cur = await db.conn.execute("SELECT COUNT(*) FROM vpn_counters")
        assert (await cur.fetchone())[0] == 0
        cur = await db.conn.execute(
            "SELECT allowed FROM vpn_chat_access WHERE chat_id = ?", (CHAT,)
        )
        assert (await cur.fetchone())[0] == 1
        # новый пир получает id после восстановленных (AUTOINCREMENT не сброшен)
        await db.conn.execute(
            "INSERT INTO vpn_peers (chat_id, device_label, public_key, address, created_at) "
            "VALUES (1, 'n', 'pubN', '10.9.0.9', 't')")
        cur = await db.conn.execute("SELECT MAX(id) FROM vpn_peers")
        assert (await cur.fetchone())[0] > 11
    finally:
        await db.close()


async def test_apply_creates_minimal_awg_conf_when_absent(world, tmp_path):
    bundle = await rs.fetch_bundle(world["ask"], "jeeves", PRIV, with_snapshot=False)
    s, cfg = _target(tmp_path)
    awg = tmp_path / "awg0.conf"
    io = FakeIO()
    await bapply.apply_bundle(bundle, s, config_path=cfg, io=io, awg_conf=awg,
                              reality_env=tmp_path / "r.env", log=lambda m: None)
    assert io.files[awg].startswith("[Interface]\n") and AWG_KEY in io.files[awg]
    assert io.restarted == []  # поднимать нечего — ставит setup-скрипт, он ключ сохранит


async def test_apply_refuses_foreign_node_and_nonempty_db(world, tmp_path):
    bundle = await rs.fetch_bundle(world["ask"], "jeeves", PRIV)
    s, cfg = _target(tmp_path, node="wooster")
    with pytest.raises(bapply.ApplyError, match="wooster"):
        await bapply.apply_bundle(bundle, s, config_path=cfg, io=FakeIO(), log=lambda m: None)
    s, cfg = _target(tmp_path)
    db = Database(s.vpn.db_path)
    await db.open()
    await apply_migrations(db)
    await _fill(db, peers=1)
    await db.close()
    io = FakeIO()
    with pytest.raises(bapply.ApplyError, match="--wipe-db"):
        await bapply.apply_bundle(bundle, s, config_path=cfg, io=io, awg_conf=tmp_path / "a",
                                  reality_env=tmp_path / "r", log=lambda m: None)
    assert io.files == {} and not publish_allowed(s)  # до отказа ничего не писали
    await bapply.apply_bundle(bundle, s, config_path=cfg, io=io, awg_conf=tmp_path / "a",
                              reality_env=tmp_path / "r", wipe_db=True, log=lambda m: None)
    assert publish_allowed(s)


async def test_import_skips_unknown_columns(tmp_path):
    db = Database(tmp_path / "x.sqlite")
    await db.open()
    await apply_migrations(db)
    snap = {"tables": {"vpn_chat_access": {
        "columns": ["chat_id", "allowed", "updated_at", "future_col"],
        "rows": [[5, 1, "t", "zzz"]]}}}
    async with db.transaction() as conn:
        done = await bapply.import_snapshot(conn, snap, wipe=False)
    assert done == {"vpn_chat_access": 1}
    cur = await db.conn.execute("SELECT chat_id, allowed FROM vpn_chat_access")
    assert [tuple(r) for r in await cur.fetchall()] == [(5, 1)]
    await db.close()


def test_patch_awg_conf_adds_missing_keys():
    awg = {"private_key": "K", "obfuscation": {"jc": 1, "h1": 9}}
    out = bapply.patch_awg_conf("[Interface]\nAddress = 1\n[Peer]\nX = 1\n", awg)
    assert out == "[Interface]\nAddress = 1\nPrivateKey = K\nJc = 1\nH1 = 9\n[Peer]\nX = 1\n"


# --- защита от затирания ----------------------------------------------------------------


async def test_publish_held_until_marker(tmp_path):
    s = _settings(tmp_path, "jeeves", "wooster", release=False)

    class Awg:
        async def __call__(self):
            return AWG_KEY

    inst = InstanceStore(tmp_path / "instances", "jeeves")
    pub = IdentityPublisher(s, inst, "jeeves", read_awg_private_key=Awg(), read_xray=lambda: XRAY)
    assert await pub.publish_if_changed() is False
    assert inst.package_path("vpn-identity", "jeeves").exists() is False
    db = Database(tmp_path / "v.sqlite")
    await db.open()
    await apply_migrations(db)
    src = SnapshotSource(s, lambda: db.conn, "jeeves")
    from sa_home_bot.backup.snapshot import SnapshotError

    with pytest.raises(SnapshotError, match="backup-publish.ok"):
        await src.handle_get({})
    allow_publish(s, "test")
    assert await pub.publish_if_changed() is True
    assert (await src.handle_get({}))["sealed"]
    await db.close()


# --- полный round-trip ------------------------------------------------------------------


async def test_round_trip_jeeves_to_wooster_and_back(world, tmp_path):
    bundle = await rs.fetch_bundle(world["ask"], "jeeves", PRIV)
    wire = rs.bundle_to_bytes(bundle)  # так бандл едет по ssh
    staged = tmp_path / "staged.json"
    staged.write_bytes(wire)
    os.chmod(staged, 0o600)
    loaded = rs.load_bundle(staged)
    s, cfg = _target(tmp_path)
    awg = tmp_path / "awg0.conf"
    io = FakeIO()
    await bapply.apply_bundle(loaded, s, config_path=cfg, io=io, awg_conf=awg,
                              reality_env=tmp_path / "r.env", log=lambda m: None)
    # тот же ключ сервера: публичный ключ из восстановленного файла == исходный
    restored_priv = [ln.split(" = ")[1] for ln in io.files[awg].splitlines()
                     if ln.startswith("PrivateKey")][0]
    assert rs.awg_public_key(restored_priv) == rs.awg_public_key(AWG_KEY)
    # и те же строки БД
    from sa_home_bot.backup.snapshot import dump_tables

    db = Database(s.vpn.db_path)
    await db.open()
    try:
        new = await dump_tables(db.conn)
    finally:
        await db.close()
    old = await dump_tables(world["db"].conn)
    for table in old:
        if table == "vpn_counters":
            assert new[table]["rows"] == []
        else:
            assert new[table] == old[table], table
