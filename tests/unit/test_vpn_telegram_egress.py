"""VpnService: telegram_egress (этап 52) — SOCKS-адрес своей ноды и проба
api.telegram.org через её reality, снятая наблюдателем-ботом."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.protocol import TELEGRAM_EGRESS_TARGET, TELEGRAM_EGRESS_TRANSPORT
from sa_home_bot.vpn.service import CHECK_STALE_FACTOR, VpnService


class DummyAwg:
    async def server_public_key(self) -> str:
        return "server-pubkey"


async def _noop_emit(event_type: str, data: dict) -> None:
    pass


async def _make(tmp_path, **cfg_kw):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    cfg = VpnConfig(location="🇳🇱 Амстердам", **cfg_kw)
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), _noop_emit)
    return svc, db, cfg


@pytest_asyncio.fixture
async def env(tmp_path):
    svc, db, cfg = await _make(tmp_path, socks_host="100.109.139.95", socks_port=1080)
    yield svc, db, cfg
    await db.close()


async def _put(
    db,
    svc,
    *,
    node="alfred",
    server=None,
    transport=TELEGRAM_EGRESS_TRANSPORT,
    target=TELEGRAM_EGRESS_TARGET,
    ok=True,
    ms=180,
    age_s=0,
):
    seen = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    await db.conn.execute(
        "INSERT INTO vpn_check_states (node, server, transport, target, status, last_ok, "
        "last_latency_ms, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node,
            server or svc._node,
            transport,
            target,
            "ok" if ok else "alerting",
            int(ok),
            ms,
            seen,
            seen,
        ),
    )
    await db.conn.commit()


async def _ask(svc, observer="alfred"):
    return await svc.run_command(vpn_protocol.ACTION_TELEGRAM_EGRESS, {"observer": observer})


async def test_declared_in_describe(env):
    svc, _, _ = env
    desc = svc.describe()
    desc = await desc if hasattr(desc, "__await__") else desc
    assert vpn_protocol.ACTION_TELEGRAM_EGRESS in desc.capabilities
    assert any(a.id == vpn_protocol.ACTION_TELEGRAM_EGRESS for a in desc.actions)


async def test_row_fresh_with_socks(env):
    svc, db, _ = env
    await _put(db, svc, ok=True, ms=180)
    res = await _ask(svc)
    assert res["node"] == svc._node
    assert res["label"] == "🇳🇱 Амстердам"
    assert res["socks"] == "100.109.139.95:1080"
    assert res["check"]["ok"] is True
    assert res["check"]["ms"] == 180
    assert res["check"]["stale"] is False
    assert res["check"]["seen_at"]


async def test_failed_check_reports_not_ok(env):
    svc, db, _ = env
    await _put(db, svc, ok=False, ms=None)
    res = await _ask(svc)
    assert res["check"]["ok"] is False
    assert res["check"]["ms"] is None


async def test_no_socks_host_gives_null(tmp_path):
    svc, db, _ = await _make(tmp_path)
    try:
        await _put(db, svc)
        res = await _ask(svc)
        assert res["socks"] is None
        assert res["check"] is not None
    finally:
        await db.close()


async def test_no_row_gives_null_check(env):
    svc, _, _ = env
    res = await _ask(svc)
    assert res["check"] is None
    assert res["socks"] == "100.109.139.95:1080"


async def test_stale_row_is_marked_stale(env):
    svc, db, cfg = env
    await _put(db, svc, age_s=cfg.check_interval_s * CHECK_STALE_FACTOR + 60)
    res = await _ask(svc)
    assert res["check"]["stale"] is True
    assert res["check"]["ok"] is True


async def test_other_transport_target_observer_server_not_mixed(env):
    svc, db, _ = env
    await _put(db, svc, transport="awg", ms=1)
    await _put(db, svc, target="https://1.1.1.1", ms=2)
    await _put(db, svc, node="wooster", ms=3)
    await _put(db, svc, server="someone-else", ms=4)
    assert (await _ask(svc))["check"] is None

    await _put(db, svc, ms=777)
    assert (await _ask(svc))["check"]["ms"] == 777
    # другой наблюдатель видит только свою строку
    assert (await _ask(svc, "wooster"))["check"]["ms"] == 3


async def test_observer_required(env):
    svc, _, _ = env
    with pytest.raises(ProtoError):
        await svc.run_command(vpn_protocol.ACTION_TELEGRAM_EGRESS, {})
