"""VpnService: report_check пишет в vpn_check_states и алертит только на
переходе статуса (мут повторов), check_now/check_status — обвязка вокруг
этого же состояния."""

from __future__ import annotations

import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import Address
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

    cfg = VpnConfig(check_fail_threshold=2, check_clear_threshold=1, check_targets=[TARGET])
    node_link = FakeNodeLink()
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit, node_link=node_link)
    yield svc, events, node_link
    await db.close()


async def _report(svc, node: str, ok: bool, error: str | None = None):
    return await svc.run_command(
        vpn_protocol.ACTION_REPORT_CHECK,
        {"node": node, "results": {TARGET: {"ok": ok, "ms": 12, "error": error}}},
    )


async def test_single_failure_does_not_alert(env):
    svc, events, _ = env
    await _report(svc, "jeeves", False, "timeout")
    assert not any(name == vpn_protocol.EVENT_VPN_CHECK_FAILED for name, _ in events)


async def test_alerts_only_once_after_threshold_then_mutes(env):
    svc, events, _ = env
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", False, "timeout")  # второй подряд — порог достигнут
    failed = [d for name, d in events if name == vpn_protocol.EVENT_VPN_CHECK_FAILED]
    assert len(failed) == 1
    assert failed[0]["node"] == "jeeves"
    assert failed[0]["target"] == TARGET

    # Ещё несколько неуспешных тиков подряд — новых алертов быть не должно (мут).
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", False, "timeout")
    failed_after = [d for name, d in events if name == vpn_protocol.EVENT_VPN_CHECK_FAILED]
    assert len(failed_after) == 1


async def test_recovery_emits_once(env):
    svc, events, _ = env
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", True)
    recovered = [d for name, d in events if name == vpn_protocol.EVENT_VPN_CHECK_RECOVERED]
    assert len(recovered) == 1
    assert recovered[0] == {"node": "jeeves", "target": TARGET}


async def test_independent_nodes_do_not_interfere(env):
    svc, events, _ = env
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "alfred", False, "timeout")
    # По одному провалу на каждую ноду — порог 2 не достигнут ни для одной.
    assert not any(name == vpn_protocol.EVENT_VPN_CHECK_FAILED for name, _ in events)


async def test_check_status_reflects_latest_state(env):
    svc, _, _ = env
    await _report(svc, "jeeves", False, "timeout")
    await _report(svc, "jeeves", False, "timeout")
    status = await svc.run_command(vpn_protocol.ACTION_CHECK_STATUS, {})
    states = status["states"]
    assert len(states) == 1
    assert states[0]["node"] == "jeeves"
    assert states[0]["target"] == TARGET
    assert states[0]["status"] == "alerting"
    assert states[0]["consecutive_count"] == 0  # сброс на самом переходе


async def test_check_now_dispatches_via_node_link(env):
    svc, _, node_link = env
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_NOW, {})
    assert result["dispatched_to"] == ["jeeves", "alfred"]
    assert len(node_link.calls) == 1
    call = node_link.calls[0]
    assert call["action"] == "trigger_peers"
    assert call["args"]["service"] == "vpn_check"
    assert call["args"]["action"] == "check"
    assert call["args"]["args"]["targets"] == [TARGET]
    assert call["dst"] == Address(node=svc._node, service="node")


async def test_check_now_without_node_link_does_not_raise(tmp_path):
    db = Database(tmp_path / "vpn2.sqlite")
    await db.open()
    await apply_migrations(db)

    async def emit(event_type, data):
        pass

    svc = VpnService(Settings(vpn=VpnConfig()), db, DummyAwg(), emit)  # node_link=None
    result = await svc.run_command(vpn_protocol.ACTION_CHECK_NOW, {})
    assert result["dispatched_to"] == []
    await db.close()
