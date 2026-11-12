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


def _outages(events: list[tuple[str, dict]]) -> list[dict]:
    return [data for name, data in events if name == vpn_protocol.EVENT_VPN_OUTAGE]


async def test_below_threshold_stays_quiet(env):
    svc, events, _ = env
    await _report(svc, "jeeves", True)
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", False, "timeout")
    assert _links(events) == []


async def test_down_reaching_threshold_announces_once(env):
    svc, events, _ = env
    await _report(svc, "alfred", True)
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
    await _report(svc, "alfred", True)
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
    await _report(svc, "alfred", True)
    await _report(svc, "jeeves", True)
    for _ in range(3):
        await _report(svc, "jeeves", False, "timeout")
    assert not any(name == vpn_protocol.EVENT_VPN_CHECK_FAILED for name, _ in events)
    assert len(_links(events)) == 1


async def test_target_events_on_by_default(env):
    svc, events, _ = env
    await _report(svc, "alfred", True)
    await _report(svc, "jeeves", True)
    for _ in range(3):
        await _report(svc, "jeeves", False, "timeout")
    assert len(_links(events)) == 1
    assert any(name == vpn_protocol.EVENT_VPN_CHECK_FAILED for name, _ in events)


@pytest_asyncio.fixture
async def scope_env(tmp_path):
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
        russia_observers=["osip"],
        ignored_observers=["zakhar"],
    )
    node_link = FakeNodeLink()
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit, node_link=node_link)
    yield svc, events, node_link
    await db.close()


async def test_russia_observer_edge_gives_outage_instead_of_link(scope_env):
    svc, events, _ = scope_env
    await _report(svc, "osip", True)
    for _ in range(3):
        await _report(svc, "osip", False, "timeout")
    outages = _outages(events)
    assert len(outages) == 1
    assert outages[0]["scope"] == "russia"
    assert outages[0]["from"] == "ok"
    assert outages[0]["to"] == "down"
    assert outages[0]["server"] == "jeeves"
    assert outages[0]["scopes"]["russia"]["jeeves"] == "down"
    assert not _links(events)


async def test_russia_observer_recovery_gives_second_outage(scope_env):
    svc, events, _ = scope_env
    await _report(svc, "osip", True)
    for _ in range(3):
        await _report(svc, "osip", False, "timeout")
    for _ in range(3):
        await _report(svc, "osip", True)
    outages = _outages(events)
    assert len(outages) == 2
    assert outages[1]["scope"] == "russia"
    assert outages[1]["from"] == "down"
    assert outages[1]["to"] == "ok"
    assert not _links(events)


async def test_live_second_link_keeps_scope_ok_and_emits_link(scope_env):
    svc, events, _ = scope_env
    await _report(svc, "alfred", True)
    await _report(svc, "wooster", True)
    for _ in range(3):
        await _report(svc, "alfred", False, "timeout")
    assert not _outages(events)
    links = _links(events)
    assert len(links) == 1
    assert links[0]["node"] == "alfred"
    assert links[0]["scope"] == "global"
    assert links[0]["scopes"]["global"]["jeeves"] == "ok"


async def test_ignored_observer_gives_no_events(scope_env):
    svc, events, _ = scope_env
    await _report(svc, "zakhar", True)
    for _ in range(3):
        await _report(svc, "zakhar", False, "timeout")
    assert not _outages(events)
    assert not _links(events)


async def test_check_scopes_returns_scopes_and_links(scope_env):
    svc, _, _ = scope_env
    await _report(svc, "osip", True)
    await _report(svc, "alfred", True)
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_SCOPES, {})
    assert set(result["scopes"]) == {"global", "russia"}
    assert result["scopes"]["russia"]["jeeves"] == "ok"
    assert result["russia"] == ["osip"]
    assert result["ignored"] == ["zakhar"]
    assert isinstance(result["links"], list)
    row = result["links"][0]
    assert row["node"] == "alfred"
    assert row["server"] == "jeeves"
    assert row["transport"] == "awg"
    assert row["level"] == "ok"
    assert "announced" in row


async def _report_net(svc, node: str, ok: bool, net_ok: bool, *, server: str = "jeeves"):
    return await svc.run_command(
        vpn_protocol.ACTION_REPORT_CHECK,
        {
            "node": node,
            "results": [
                {
                    "server": server,
                    "transport": "awg",
                    "target": TARGET,
                    "ok": ok,
                    "ms": 12,
                    "error": None if ok else "timeout",
                }
            ],
            "net": {server: {"ok": net_ok, "ms": 40 if net_ok else None, "error": None}},
        },
    )


def _layer(events: list[tuple[str, dict]], layer: str) -> list[dict]:
    return [data for data in _outages(events) if data.get("layer") == layer]


async def test_net_failure_gives_net_outage_and_freezes_link(scope_env):
    svc, events, _ = scope_env
    await _report_net(svc, "osip", True, True)
    for _ in range(3):
        await _report_net(svc, "osip", False, False)
    assert [(o["scope"], o["server"], o["to"]) for o in _layer(events, "net")] == [
        ("russia", "jeeves", "down")
    ]
    assert _layer(events, "vpn") == []
    assert _links(events) == []
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_SCOPES, {})
    assert result["links"][0]["level"] == "ok"
    assert result["scopes"]["russia"]["jeeves"] == "down"
    assert result["layers"]["russia"]["jeeves"] == {"net": "down", "node": None, "vpn": "ok"}


async def test_net_recovery_is_announced_on_net_layer(scope_env):
    svc, events, _ = scope_env
    await _report_net(svc, "osip", True, True)
    for _ in range(3):
        await _report_net(svc, "osip", False, False)
    for _ in range(3):
        await _report_net(svc, "osip", True, True)
    assert [o["to"] for o in _layer(events, "net")] == ["down", "ok"]
    assert _layer(events, "vpn") == []


async def test_first_net_report_is_silent_even_when_down(scope_env):
    svc, events, _ = scope_env
    for _ in range(4):
        await _report_net(svc, "osip", False, False)
    assert _outages(events) == []
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_SCOPES, {})
    assert result["layers"]["russia"]["jeeves"]["net"] == "down"


async def test_vpn_outage_with_live_net_is_marked_as_vpn_layer(scope_env):
    svc, events, _ = scope_env
    await _report_net(svc, "osip", True, True)
    for _ in range(3):
        await _report_net(svc, "osip", False, True)
    assert [o["to"] for o in _layer(events, "vpn")] == ["down"]
    assert _layer(events, "net") == []


async def _report_targets(svc, node: str, oks: dict[str, bool], *, transport: str = "awg"):
    return await svc.run_command(
        vpn_protocol.ACTION_REPORT_CHECK,
        {
            "node": node,
            "results": [
                {
                    "server": "jeeves",
                    "transport": transport,
                    "target": target,
                    "ok": ok,
                    "ms": 20,
                    "error": None if ok else "timeout",
                }
                for target, ok in oks.items()
            ],
        },
    )


async def test_target_bad_through_all_working_links_is_left_out_of_level(scope_env):
    svc, _, _ = scope_env
    dead = "https://dead.example"
    await _report_targets(svc, "alfred", {TARGET: True, dead: False})
    await _report_targets(svc, "wooster", {TARGET: True, dead: False})
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_SCOPES, {})
    levels = {row["node"]: row["level"] for row in result["links"]}
    assert levels == {"alfred": "partial", "wooster": "ok"}
    assert [row["target"] for row in result["resources"]] == [dead]


async def _watch(svc, monkeypatch, live: list[str], times: int) -> None:
    async def live_vpn_nodes(_node_link):
        return live

    monkeypatch.setattr("sa_home_bot.vpn.checks.vpn_nodes.live_vpn_nodes", live_vpn_nodes)
    for _ in range(times):
        await svc._watch_servers()


async def test_silent_neighbour_gives_node_outage_and_checks_on_its_behalf(scope_env, monkeypatch):
    svc, events, node_link = scope_env
    svc._node = "wooster"
    await _report(svc, "alfred", True)
    await _watch(svc, monkeypatch, ["wooster", "jeeves"], 1)
    assert node_link.calls == []
    await _watch(svc, monkeypatch, ["wooster"], 3)
    assert _layer(events, "node") == []
    await _watch(svc, monkeypatch, ["wooster"], 1)
    down = _layer(events, "node")
    assert [(o["server"], o["scope"], o["to"]) for o in down] == [("jeeves", None, "down")]
    assert down[0]["scopes"]["global"]["jeeves"] == "down"
    assert {call["args"]["args"]["server"] for call in node_link.calls} == {"jeeves"}
    await _watch(svc, monkeypatch, ["wooster", "jeeves"], 4)
    assert [o["to"] for o in _layer(events, "node")] == ["down", "ok"]


async def test_node_outage_is_muted_when_net_is_already_down(scope_env, monkeypatch):
    svc, events, _ = scope_env
    svc._node = "wooster"
    await _report_net(svc, "alfred", True, True)
    for _ in range(3):
        await _report_net(svc, "alfred", False, False)
    await _watch(svc, monkeypatch, ["wooster"], 4)
    await _watch(svc, monkeypatch, ["wooster", "jeeves"], 4)
    assert _layer(events, "node") == []
    assert [o["to"] for o in _layer(events, "net")] == ["down"]


async def test_watch_is_skipped_when_own_node_is_not_seen(scope_env, monkeypatch):
    svc, events, node_link = scope_env
    svc._node = "wooster"
    await _report(svc, "alfred", True)
    await _watch(svc, monkeypatch, [], 5)
    assert _outages(events) == []
    assert node_link.calls == []


async def test_link_counters_stand_still_while_node_is_down(scope_env, monkeypatch):
    svc, events, _ = scope_env
    svc._node = "wooster"
    await _report(svc, "alfred", True)
    await _watch(svc, monkeypatch, ["wooster"], 4)
    for _ in range(3):
        await _report(svc, "alfred", False, "timeout")
    assert _links(events) == []
    assert _layer(events, "vpn") == []


async def test_own_dispatch_carries_net_target_and_foreign_does_not(tmp_path):
    from sa_home_bot.config import RealityTransportConfig

    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(event_type: str, data: dict) -> None:
        return None

    cfg = VpnConfig(
        check_targets=[TARGET],
        endpoint_host="203.0.113.5",
        reality=RealityTransportConfig(port=8443),
    )
    node_link = FakeNodeLink()
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit, node_link=node_link)
    await svc._dispatch_checks()
    await svc._dispatch_checks("jeeves")
    await db.close()
    own, foreign = (call["args"]["args"] for call in node_link.calls)
    assert own["net_target"] == {"host": "203.0.113.5", "port": 8443}
    assert "net_target" not in foreign


async def test_own_node_layer_is_ok(scope_env):
    svc, _, _ = scope_env
    svc._node = "jeeves"
    await _report(svc, "alfred", True)
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_SCOPES, {})
    assert result["layers"]["global"]["jeeves"]["node"] == "ok"
