"""VpnService: report_check пишет в vpn_check_states и алертит только на
переходе статуса (мут повторов), check_now/check_status — обвязка вокруг
этого же состояния."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import Address
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.service import CHECK_STALE_FACTOR, VpnService

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
    assert recovered[0] == {
        "node": "jeeves",
        "server": "jeeves",
        "transport": "awg",
        "target": TARGET,
    }


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
    assert states[0]["server"] == "jeeves"
    assert states[0]["transport"] == "awg"
    assert states[0]["target"] == TARGET
    assert states[0]["status"] == "alerting"
    assert states[0]["consecutive_count"] == 0  # сброс на самом переходе
    assert status["rollup"] == [
        {"server": "jeeves", "transport": "awg", "status": "alerting", "observers": 1}
    ]


async def test_check_status_rollup_partial_when_observers_disagree(env):
    svc, _, _ = env
    # Один и тот же (server, transport), но два разных наблюдателя (node) —
    # один видит ok, другой alerting → роллап «partial».
    await _report(svc, "jeeves", True, server="wooster", transport="reality")
    await _report(svc, "alfred", False, "blocked", server="wooster", transport="reality")
    await _report(svc, "alfred", False, "blocked", server="wooster", transport="reality")
    status = await svc.run_command(vpn_protocol.ACTION_CHECK_STATUS, {})
    assert status["rollup"] == [
        {"server": "wooster", "transport": "reality", "status": "partial", "observers": 2}
    ]


async def test_usage_carries_own_check_rollup(env):
    """Индикатор для карточки /vpn едет вместе с расходом (39.0.7(f)): своя
    пара — в ответе, чужие — нет, про них спросят их собственный сервер."""
    svc, _, _ = env
    await _report(svc, "alfred", True, server=svc._node, transport="awg")
    await _report(svc, "alfred", False, "blocked", server="wooster", transport="reality")
    usage = await svc.run_command(vpn_protocol.ACTION_USAGE, {"chat_id": 42})
    assert usage["check"] == [
        {"server": svc._node, "transport": "awg", "status": "ok", "observers": 1}
    ]


async def test_get_state_carries_own_check_rollup(env):
    """Пикер локации строится из get_state (bot/vpn_nodes.live_vpn_servers) —
    индикатор должен быть и там, не только в usage."""
    svc, _, _ = env
    await _report(svc, "alfred", True, server=svc._node, transport="awg")
    state = await svc.get_state()
    assert state["check"] == [
        {"server": svc._node, "transport": "awg", "status": "ok", "observers": 1}
    ]


async def test_stale_observer_drops_out_of_rollup(env):
    """Наблюдатель замолчал (его нода умерла) — его последний «ok» не должен
    вечно красить индикатор зелёным: строка старше CHECK_STALE_FACTOR циклов
    из сводки выпадает. Сырые states при этом остаются — админу видно всё."""
    svc, _, _ = env
    await _report(svc, "alfred", True, server="jeeves", transport="awg")
    stale = (
        datetime.now(UTC) - timedelta(seconds=svc._cfg.check_interval_s * CHECK_STALE_FACTOR + 60)
    ).isoformat()
    await svc._db.conn.execute("UPDATE vpn_check_states SET last_seen_at = ?", (stale,))
    await svc._db.conn.commit()
    status = await svc.run_command(vpn_protocol.ACTION_CHECK_STATUS, {})
    assert status["rollup"] == []
    assert len(status["states"]) == 1


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
    assert call["args"]["args"]["server"] == svc._node
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
