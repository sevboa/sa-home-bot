from __future__ import annotations

import json

import pytest
import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.service import VpnService
from tests.unit.test_vpn_service_report_check import DummyAwg, FakeNodeLink


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    svc = VpnService(Settings(vpn=VpnConfig()), db, DummyAwg(), emit, node_link=FakeNodeLink())
    yield svc, events, db
    await db.close()


def _result(raw: str = "ok", series: tuple[str, ...] = ("ok",), transport: str = "reality"):
    return {
        "server": "jeeves",
        "transport": transport,
        "mode": "single",
        "raw": {"verdict": raw},
        "series": [{"verdict": v} for v in series],
        "at": "2026-11-03T00:00:00+00:00",
    }


async def _report(svc, *results):
    return await svc.run_command(
        vpn_protocol.ACTION_REPORT_SERIES, {"node": "zakhar", "results": list(results)}
    )


async def test_two_failures_emit_failed_once_then_recovered(env):
    svc, events, db = env
    await _report(svc, _result(series=("freeze",)))
    assert events == []
    await _report(svc, _result(series=("freeze",)))
    await _report(svc, _result(series=("freeze",)))
    assert events == [
        (
            vpn_protocol.EVENT_VPN_SERIES_FAILED,
            {
                "node": "zakhar",
                "server": "jeeves",
                "transport": "reality",
                "verdict": "freeze",
                "at_bytes": None,
                "consecutive": 2,
            },
        )
    ]
    await _report(svc, _result(series=("slow",)))
    assert events[-1] == (
        vpn_protocol.EVENT_VPN_SERIES_RECOVERED,
        {"node": "zakhar", "server": "jeeves", "transport": "reality"},
    )
    cur = await db.conn.execute("SELECT COUNT(*) AS n FROM vpn_check_states")
    assert (await cur.fetchone())["n"] == 0


async def test_state_keeps_last_report_per_transport(env):
    svc, _, db = env
    await _report(svc, _result(raw="tcp_blocked", series=()), _result(transport="awg"))
    cur = await db.conn.execute(
        "SELECT transport, status, last_verdict, last_report FROM vpn_series_states "
        "ORDER BY transport"
    )
    rows = [dict(r) for r in await cur.fetchall()]
    assert [(r["transport"], r["last_verdict"]) for r in rows] == [
        ("awg", "ok"),
        ("reality", "tcp_blocked"),
    ]
    assert json.loads(rows[1]["last_report"])["raw"]["verdict"] == "tcp_blocked"
    status = await svc.run_command(vpn_protocol.ACTION_CHECK_STATUS, {})
    assert [(r["transport"], r["status"], r["last_verdict"]) for r in status["series"]] == [
        ("awg", "ok", "ok"),
        ("reality", "ok", "tcp_blocked"),
    ]


@pytest.mark.parametrize("args", [{}, {"node": "zakhar"}, {"node": "zakhar", "results": []}])
async def test_bad_request(env, args):
    svc, _, _ = env
    with pytest.raises(ProtoError) as exc:
        await svc.run_command(vpn_protocol.ACTION_REPORT_SERIES, args)
    assert exc.value.code == ERR_BAD_REQUEST
