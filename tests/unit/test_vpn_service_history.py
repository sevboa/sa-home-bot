from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.service import VpnService

TARGET = "https://1.1.1.1"


class DummyAwg:
    async def server_public_key(self) -> str:
        return "server-pubkey"

    async def add_peer(self, public_key: str, address: str) -> None:
        pass

    async def remove_peer(self, public_key: str) -> None:
        pass

    async def transfer(self) -> dict:
        return {}

    async def latest_handshakes(self) -> dict:
        return {}

    async def generate_keypair(self) -> tuple[str, str]:
        return "priv", "pub"


class FakeNodeLink:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append({"action": action, "args": args, "dst": dst})
        return {"dispatched": ["jeeves", "alfred"], "skipped": [], "unreachable": []}


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        check_fail_threshold=2,
        check_clear_threshold=1,
        check_targets=[TARGET],
        link_stable_threshold=3,
    )
    node_link = FakeNodeLink()
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit, node_link=node_link)
    yield svc, events, node_link
    await db.close()


async def _report(
    svc,
    node: str,
    ok: bool,
    error: str | None = None,
    *,
    server: str = "jeeves",
    transport: str = "awg",
):
    return await svc.run_command(
        vpn_protocol.ACTION_REPORT_CHECK,
        {
            "node": node,
            "results": [
                {
                    "server": server,
                    "transport": transport,
                    "target": TARGET,
                    "ok": ok,
                    "ms": 12,
                    "error": error,
                }
            ],
        },
    )


async def _samples(svc) -> list[dict]:
    cur = await svc._db.conn.execute(
        "SELECT at, node, server, transport, total, bad, median_ms FROM vpn_check_samples "
        "ORDER BY at, node"
    )
    return [dict(row) for row in await cur.fetchall()]


async def test_report_writes_one_sample_per_link(env):
    svc, _, _ = env
    await svc.run_command(
        vpn_protocol.ACTION_REPORT_CHECK,
        {
            "node": "alfred",
            "results": [
                {"server": "jeeves", "transport": "awg", "target": TARGET, "ok": True, "ms": 100},
                {"server": "jeeves", "transport": "awg", "target": "https://9.9.9.9", "ok": False},
            ],
        },
    )
    rows = await _samples(svc)
    assert len(rows) == 1
    assert rows[0]["node"] == "alfred"
    assert rows[0]["server"] == "jeeves"
    assert rows[0]["transport"] == "awg"
    assert rows[0]["total"] == 2
    assert rows[0]["bad"] == 1
    assert rows[0]["median_ms"] == 100


async def test_sample_without_healthy_targets_has_no_median(env):
    svc, _, _ = env
    await _report(svc, "alfred", False, "timeout")
    rows = await _samples(svc)
    assert len(rows) == 1
    assert rows[0]["total"] == 1
    assert rows[0]["bad"] == 1
    assert rows[0]["median_ms"] is None


async def test_check_history_returns_series(env):
    svc, _, _ = env
    await _report(svc, "alfred", True)
    await _report(svc, "alfred", False, "timeout")
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_HISTORY, {})
    assert result["hours"] == 24
    assert result["bucket_min"] == 15
    series = result["series"]
    assert len(series) == 1
    assert series[0]["node"] == "alfred"
    assert series[0]["server"] == "jeeves"
    assert series[0]["transport"] == "awg"
    assert sum(point["reports"] for point in series[0]["points"]) == 2


async def test_check_history_rejects_non_integer_hours(env):
    svc, _, _ = env
    with pytest.raises(ProtoError) as caught:
        await svc.run_command(vpn_protocol.ACTION_CHECK_HISTORY, {"hours": "abc"})
    assert caught.value.code == ERR_BAD_REQUEST
    assert caught.value.message == "hours и bucket_min — целые числа"


async def test_prune_samples_keeps_only_recent(env):
    svc, _, _ = env
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    await svc._db.conn.execute(
        "INSERT INTO vpn_check_samples (at, node, server, transport, total, bad, median_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (old, "alfred", "jeeves", "awg", 1, 0, 100),
    )
    await svc._db.conn.execute(
        "INSERT INTO vpn_check_samples (at, node, server, transport, total, bad, median_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fresh, "alfred", "jeeves", "awg", 1, 0, 100),
    )
    await svc._db.conn.commit()
    await svc._prune_samples()
    rows = await _samples(svc)
    assert len(rows) == 1
    assert rows[0]["at"] == fresh
