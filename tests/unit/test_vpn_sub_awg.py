from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import parse_qs, quote, urlsplit

import pytest
import pytest_asyncio

from sa_home_bot.config import (
    AwgAltConfig,
    RealityTransportConfig,
    Settings,
    SwarmConfig,
    VpnConfig,
)
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.awg import DualAwgBackend
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID, TRANSPORT_AWG, TRANSPORT_REALITY
from sa_home_bot.vpn.service import VpnService

from .test_vpn_awg_alt_peers import FakeAwg
from .test_vpn_service import CHAT, FakeXray, allow

LABEL = "📱 iPhone"


async def _noop(*_a) -> None:
    return None


async def _make(tmp_path, with_alt: bool):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    alt = AwgAltConfig(interface="awg1", port=51821, subnet="10.9.1.0/24") if with_alt else None
    cfg = VpnConfig(
        subnet="10.9.0.0/24",
        base_quota_gb=1,
        endpoint_host="203.0.113.9",
        transports=[TRANSPORT_AWG, TRANSPORT_REALITY],
        location="🇳🇱 Нидерланды",
        reality=RealityTransportConfig(
            endpoint_host="203.0.113.9", server_public_key="PUB", short_id="abcd1234"
        ),
        sub_port=18444,
        awg_alt=alt,
    )
    primary, second = FakeAwg("primary-pub"), FakeAwg("second-pub")
    backend = DualAwgBackend(primary, second, "10.9.1.0/24") if with_alt else primary
    svc = VpnService(
        Settings(vpn=cfg, swarm=SwarmConfig(token="tok")),
        db,
        backend,
        _noop,
        reality_backend=FakeXray(),
    )
    svc._node_id = "jeeves"
    await allow(svc, CHAT)
    await svc.run_command(
        vpn_protocol.ACTION_ISSUE,
        {"chat_id": CHAT, "device_label": LABEL, "transport": TRANSPORT_REALITY},
    )
    return svc, db, primary, second


@pytest_asyncio.fixture(params=[True, False], ids=["alt", "single"])
async def env(tmp_path, request):
    svc, db, primary, second = await _make(tmp_path, request.param)
    yield svc, request.param, primary, second
    await db.close()


def _token(svc):
    return subs.make_token(svc._sub_secret, CHAT, LABEL)


async def _sub_rows(svc, status="active"):
    cur = await svc._db.conn.execute(
        "SELECT * FROM vpn_peers WHERE purpose = 'sub' AND status = ?", (status,)
    )
    return await cur.fetchall()


async def test_lazy_issue_creates_one_peer_per_version_once(env):
    svc, with_alt, primary, second = env
    reply = await svc._sub_links({"token": _token(svc)})
    names = [e["name"] for e in reply["awg_entries"]]
    assert names == (["🇳🇱 AWG 1.0", "🇳🇱 AWG 2.0"] if with_alt else ["🇳🇱 AWG 1.0"])
    rows = await _sub_rows(svc)
    assert len(rows) == len(names)
    assert all(r["private_key"] and r["transport"] == "awg" for r in rows)
    assert len(primary.peers) == 1
    assert len(second.peers) == (1 if with_alt else 0)
    again = await svc._sub_links({"token": _token(svc)})
    assert again["awg_entries"] == reply["awg_entries"]
    assert len(await _sub_rows(svc)) == len(names)


async def test_versions_use_their_subnet_and_obfuscation(env):
    svc, with_alt, _p, _s = env
    reply = await svc._sub_links({"token": _token(svc)})
    first = subs.AwgSubEntry.from_wire(reply["awg_entries"][0])
    assert first.address.startswith("10.9.0.") and first.port == svc._cfg.endpoint_port
    assert [k for k, _v in first.obfuscation][:3] == ["jc", "jmin", "jmax"]
    if with_alt:
        second = subs.AwgSubEntry.from_wire(reply["awg_entries"][1])
        assert second.address.startswith("10.9.1.") and second.port == 51821
        assert "s3" in dict(second.obfuscation)
        assert second.server_public_key == "second-pub"


async def test_concurrent_requests_make_single_set(env):
    svc, with_alt, _p, _s = env
    await asyncio.gather(*(svc._sub_links({"token": _token(svc)}) for _ in range(4)))
    assert len(await _sub_rows(svc)) == (2 if with_alt else 1)


async def test_probe_gets_no_sub_peers(env):
    svc, _alt, _p, _s = env
    assert await svc._sub_awg_entries(PROBE_CHAT_ID, LABEL) == []
    assert await _sub_rows(svc) == []


async def test_sub_peers_are_hidden_from_device_views(env):
    svc, with_alt, _p, _s = env
    await svc._sub_links({"token": _token(svc)})
    devices = await svc._peers_for_chat(CHAT)
    assert [d["transport"] for d in devices] == [TRANSPORT_REALITY]
    assert await svc._active_row(CHAT, LABEL, TRANSPORT_AWG) is None
    assert await svc._active_labels(CHAT) == {LABEL}
    peers = (await svc._peers({}))["peers"]
    assert sorted(p["purpose"] for p in peers) == ["app"] + ["sub"] * (2 if with_alt else 1)


async def test_revoking_last_app_peer_revokes_sub_peers(env):
    svc, _alt, primary, second = env
    await svc._sub_links({"token": _token(svc)})
    await svc.run_command(
        vpn_protocol.ACTION_REVOKE,
        {"chat_id": CHAT, "device_label": LABEL, "transport": TRANSPORT_REALITY},
    )
    assert await _sub_rows(svc) == []
    assert len(await _sub_rows(svc, "revoked")) >= 1
    assert primary.peers == {} and second.peers == {}


async def test_stale_server_key_reissues_sub_peer(env):
    svc, _alt, primary, _s = env
    first = (await svc._sub_links({"token": _token(svc)}))["awg_entries"][0]
    primary.server_pub = "rotated"
    svc._server_pubkey = None
    second = (await svc._sub_links({"token": _token(svc)}))["awg_entries"][0]
    assert second["server_public_key"] == "rotated"
    assert second["private_key"] != first["private_key"]


async def test_no_awg_entries_without_awg_transport(tmp_path):
    svc, db, _p, _s = await _make(tmp_path, False)
    svc._transports = (TRANSPORT_REALITY,)
    reply = await svc._sub_links({"token": _token(svc)})
    assert "awg_entries" not in reply
    await db.close()


async def test_migration_adds_columns_to_old_db(tmp_path):
    db = Database(tmp_path / "old.sqlite")
    await db.open()
    await db.conn.execute(
        "CREATE TABLE vpn_peers (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
        "device_label TEXT NOT NULL, public_key TEXT NOT NULL UNIQUE, address TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL, revoked_at TEXT, "
        "last_handshake_at TEXT)"
    )
    await db.conn.execute(
        "INSERT INTO vpn_peers (chat_id, device_label, public_key, address, created_at) "
        "VALUES (1, 'x', 'k', '10.9.0.2', 'now')"
    )
    await db.conn.commit()
    await apply_migrations(db)
    cur = await db.conn.execute("SELECT purpose, private_key FROM vpn_peers")
    row = await cur.fetchone()
    assert row["purpose"] == "app" and row["private_key"] is None
    await db.close()


def _entry(version="2.0"):
    return subs.AwgSubEntry(
        node="jeeves",
        name=f"🇳🇱 AWG {version}",
        version=version,
        private_key="pri/vate+key=",
        address="10.9.1.2",
        server_public_key="srv/pub+key=",
        endpoint_host="203.0.113.9",
        port=51821,
        dns="1.1.1.1",
        mtu=1280,
        obfuscation=(
            ("jc", "5"),
            ("jmin", "40"),
            ("s1", "50"),
            ("s3", "0"),
            ("h1", "100-200"),
            ("i1", "<b 0x01>"),
        ),
    )


def test_wire_roundtrip():
    entry = _entry()
    assert subs.AwgSubEntry.from_wire(json.loads(json.dumps(entry.to_wire()))) == entry


def _sub(awg=()):
    base = [
        subs.SubEntry(
            "jeeves",
            "🇳🇱 Reality google",
            "uuid",
            RealityParams("203.0.113.9", 8443, "PUB", "ab", "www.google.com"),
        )
    ]
    return subs.Subscription(LABEL, tuple(base), awg_entries=tuple(awg))


@pytest.mark.parametrize("fmt", [subs.FORMAT_PLAIN, subs.FORMAT_VLESS])
def test_render_has_both_awg_lines(fmt):
    body, _ = subs.render_body(_sub([_entry()]), fmt)
    text = body if fmt == subs.FORMAT_PLAIN else base64.b64decode(body).decode()
    lines = text.strip().split("\n")
    assert lines[0].startswith("vless://")
    amnezia, hiddify = urlsplit(lines[1]), urlsplit(lines[2])
    assert amnezia.scheme == "amneziawg" and hiddify.scheme == "awg"
    q1, q2 = parse_qs(amnezia.query), parse_qs(hiddify.query)
    assert q1["persistentkeepalive"] == ["25"] and "keepalive" not in q1
    assert q2["keepalive"] == ["25"] and q2["privatekey"] == ["pri/vate+key="]
    for q in (q1, q2):
        assert q["publickey"] == ["srv/pub+key="] and q["address"] == ["10.9.1.2/32"]
        assert q["allowedips"] == ["0.0.0.0/0"] and q["h1"] == ["100-200"]
        assert q["i1"] == ["<b 0x01>"] and q["s3"] == ["0"]
    assert amnezia.fragment == quote("🇳🇱 AWG 2.0", safe="")
    assert amnezia.netloc == f"{quote('pri/vate+key=', safe='')}@203.0.113.9:51821"


def test_singbox_endpoints_and_selector():
    body, _ = subs.render_body(_sub([_entry("1.0"), _entry("2.0")]), subs.FORMAT_SINGBOX)
    cfg = json.loads(body)
    ep = cfg["endpoints"][1]
    assert ep["type"] == "awg" and ep["address"] == ["10.9.1.2/32"] and ep["mtu"] == 1280
    assert ep["awg"]["jc"] == 5 and ep["awg"]["s3"] == 0
    assert ep["awg"]["h1"] == "100-200" and ep["awg"]["i1"] == "<b 0x01>"
    assert ep["peers"][0]["port"] == 51821 and ep["peers"][0]["public_key"] == "srv/pub+key="
    outs = {o["tag"]: o for o in cfg["outbounds"]}
    assert "🇳🇱 AWG 1.0" in outs["proxy"]["outbounds"]
    assert any("AWG" in t for t in outs["auto"]["outbounds"])


def test_singbox_without_awg_has_no_endpoints_key():
    body, _ = subs.render_body(_sub(), subs.FORMAT_SINGBOX)
    assert "endpoints" not in json.loads(body)
