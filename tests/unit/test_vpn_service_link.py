from __future__ import annotations

import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
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


def _links(events: list[tuple[str, dict]]) -> list[dict]:
    return [data for name, data in events if name == vpn_protocol.EVENT_VPN_LINK_CHANGED]


async def test_below_threshold_stays_quiet(env):
    svc, events, _ = env
    await _report(svc, "jeeves", True)
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", False, "timeout")
    assert _links(events) == []


async def test_down_reaching_threshold_announces_once(env):
    svc, events, _ = env
    await _report(svc, "jeeves", True)
    for _ in range(3):
        await _report(svc, "jeeves", False, "timeout")
    links = _links(events)
    assert len(links) == 1
    assert links[0]["from"] == "ok"
    assert links[0]["to"] == "down"
    assert links[0]["bad_targets"] == [TARGET]
    assert links[0]["node"] == "jeeves"
    assert links[0]["server"] == "jeeves"
    assert links[0]["transport"] == "awg"
    assert links[0]["total"] == 1


async def test_recovery_announces_second_edge(env):
    svc, events, _ = env
    await _report(svc, "jeeves", True)
    for _ in range(3):
        await _report(svc, "jeeves", False, "timeout")
    for _ in range(3):
        await _report(svc, "jeeves", True)
    links = _links(events)
    assert len(links) == 2
    assert links[1]["from"] == "down"
    assert links[1]["to"] == "ok"


async def test_debounce_never_announces(env):
    svc, events, _ = env
    for ok in (True, False, True, False, True, False):
        await _report(svc, "jeeves", ok, None if ok else "timeout")
    assert _links(events) == []


async def test_first_report_announces_nothing(env):
    svc, events, _ = env
    for _ in range(6):
        await _report(svc, "jeeves", False, "timeout")
    assert _links(events) == []


async def test_ignored_observer_writes_state_but_stays_quiet(env):
    svc, events, _ = env

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        check_fail_threshold=2,
        check_clear_threshold=1,
        check_targets=[TARGET],
        link_stable_threshold=3,
        ignored_observers=["zakhar"],
    )
    svc = VpnService(Settings(vpn=cfg), svc._db, DummyAwg(), emit)
    await _report(svc, "zakhar", True)
    for _ in range(3):
        await _report(svc, "zakhar", False, "timeout")
    assert _links(events) == []
    cur = await svc._db.conn.execute(
        "SELECT level FROM vpn_link_states WHERE node = ? AND server = ? AND transport = ?",
        ("zakhar", "jeeves", "awg"),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["level"] == "down"


async def test_target_events_can_be_switched_off(env):
    svc, events, _ = env

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    cfg = VpnConfig(
        check_fail_threshold=2,
        check_clear_threshold=1,
        check_targets=[TARGET],
        link_stable_threshold=3,
        check_target_events=False,
    )
    svc = VpnService(Settings(vpn=cfg), svc._db, DummyAwg(), emit)
    await _report(svc, "jeeves", True)
    for _ in range(3):
        await _report(svc, "jeeves", False, "timeout")
    assert not any(name == vpn_protocol.EVENT_VPN_CHECK_FAILED for name, _ in events)
    assert len(_links(events)) == 1


async def test_target_events_on_by_default(env):
    svc, events, _ = env
    await _report(svc, "jeeves", True)
    for _ in range(3):
        await _report(svc, "jeeves", False, "timeout")
    assert len(_links(events)) == 1
    assert any(name == vpn_protocol.EVENT_VPN_CHECK_FAILED for name, _ in events)
