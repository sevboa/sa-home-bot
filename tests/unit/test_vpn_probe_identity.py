from __future__ import annotations

import pytest

from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID, TRANSPORT_AWG, TRANSPORT_REALITY

from .test_vpn_service import CHAT, env_both

__all__ = ["env_both"]


async def _issue(svc, chat_id, label, transport):
    return await svc.run_command(
        vpn_protocol.ACTION_ISSUE,
        {"chat_id": chat_id, "transport": transport, "device_label": label},
    )


async def _active(svc, chat_id, label=None):
    cur = await svc._db.conn.execute(
        "SELECT device_label, transport, public_key, address FROM vpn_peers "
        "WHERE chat_id = ? AND status = 'active' ORDER BY id",
        (chat_id,),
    )
    rows = await cur.fetchall()
    return [row for row in rows if label is None or row["device_label"] == label]


@pytest.mark.parametrize("transport", [TRANSPORT_AWG, TRANSPORT_REALITY])
async def test_probe_reissue_with_same_label_replaces_old_peer(env_both, transport):
    svc, awg, xray, _events = env_both
    label = f"probe-osip-{transport}"
    first = await _issue(svc, PROBE_CHAT_ID, label, transport)
    old = (await _active(svc, PROBE_CHAT_ID, label))[0]
    second = await _issue(svc, PROBE_CHAT_ID, label, transport)
    rows = await _active(svc, PROBE_CHAT_ID, label)
    assert len(rows) == 1
    assert rows[0]["public_key"] != old["public_key"]
    assert first["device_label"] == second["device_label"] == label
    assert old["public_key"] not in awg.peers
    assert old["public_key"] not in xray.clients.values()
    assert len(awg.peers) + len(xray.clients) == 1


async def test_probe_slots_of_same_observer_do_not_revoke_each_other(env_both):
    svc, awg, xray, _events = env_both
    slots = [
        ("probe-osip-awg", TRANSPORT_AWG),
        ("probe-osip-awg_alt", TRANSPORT_AWG),
        ("probe-osip-reality", TRANSPORT_REALITY),
        ("probe-osip-reality_alt", TRANSPORT_REALITY),
        ("probe-osip-reality_xhttp", TRANSPORT_REALITY),
    ]
    for _ in range(2):
        for label, transport in slots:
            await _issue(svc, PROBE_CHAT_ID, label, transport)
    rows = await _active(svc, PROBE_CHAT_ID)
    assert sorted(row["device_label"] for row in rows) == sorted(label for label, _ in slots)
    assert len(awg.peers) == 2
    assert len(xray.clients) == 3
    assert "c0-probe-osip-reality_alt" in xray.clients


@pytest.mark.parametrize("transport", [TRANSPORT_AWG, TRANSPORT_REALITY])
async def test_regular_chat_with_same_label_still_fails(env_both, transport):
    svc, _awg, _xray, _events = env_both
    await _issue(svc, CHAT, "телефон", transport)
    with pytest.raises(ProtoError) as exc:
        await _issue(svc, CHAT, "телефон", transport)
    assert exc.value.code == ERR_BAD_REQUEST
    assert "уже выдано" in exc.value.message
    assert len(await _active(svc, CHAT)) == 1


async def test_probe_without_label_keeps_issuing_new_peers(env_both):
    svc, _awg, _xray, _events = env_both
    for _ in range(2):
        await svc.run_command(
            vpn_protocol.ACTION_ISSUE, {"chat_id": PROBE_CHAT_ID, "transport": TRANSPORT_AWG}
        )
    assert len(await _active(svc, PROBE_CHAT_ID)) == 2


async def _seed_prune_world(svc):
    await _issue(svc, PROBE_CHAT_ID, "probe-osip-awg", TRANSPORT_AWG)
    await _issue(svc, PROBE_CHAT_ID, "probe-osip-reality", TRANSPORT_REALITY)
    await _issue(svc, PROBE_CHAT_ID, "Rose", TRANSPORT_AWG)
    await _issue(svc, PROBE_CHAT_ID, "Lily2", TRANSPORT_REALITY)
    await _issue(svc, CHAT, "Rose", TRANSPORT_AWG)
    await _issue(svc, CHAT, "probeless", TRANSPORT_REALITY)
    await _issue(svc, CHAT, "Lily2", TRANSPORT_REALITY)


async def _snapshot(svc, awg, xray):
    cur = await svc._db.conn.execute("SELECT * FROM vpn_peers ORDER BY id")
    return [tuple(row) for row in await cur.fetchall()], dict(awg.peers), dict(xray.clients)


@pytest.mark.parametrize("args", [{}, {"dry_run": True}])
async def test_prune_dry_run_changes_nothing_and_lists_orphans(env_both, args):
    svc, awg, xray, _events = env_both
    await _seed_prune_world(svc)
    before = await _snapshot(svc, awg, xray)
    result = await svc.run_command(vpn_protocol.ACTION_PRUNE_PROBE_PEERS, args)
    assert result["dry_run"] is True
    assert result["count"] == 2
    assert sorted(p["device_label"] for p in result["peers"]) == ["Lily2", "Rose"]
    assert await _snapshot(svc, awg, xray) == before


async def test_prune_removes_only_unnamed_probe_peers(env_both):
    svc, awg, xray, _events = env_both
    await _seed_prune_world(svc)
    guests_before = await _active(svc, CHAT)
    result = await svc.run_command(vpn_protocol.ACTION_PRUNE_PROBE_PEERS, {"dry_run": False})
    assert result["dry_run"] is False and result["count"] == 2
    assert sorted(r["device_label"] for r in await _active(svc, PROBE_CHAT_ID)) == [
        "probe-osip-awg",
        "probe-osip-reality",
    ]
    assert [tuple(r) for r in await _active(svc, CHAT)] == [tuple(r) for r in guests_before]
    assert len(awg.peers) == 2
    assert sorted(xray.clients) == [
        "c0-probe-osip-reality",
        f"c{CHAT}-Lily2",
        f"c{CHAT}-probeless",
    ]
    again = await svc.run_command(vpn_protocol.ACTION_PRUNE_PROBE_PEERS, {"dry_run": False})
    assert again["count"] == 0


async def test_prune_action_is_not_offered_in_menus_but_still_runs(env_both):
    svc, _awg, _xray, _events = env_both
    description = svc.describe()
    assert vpn_protocol.ACTION_PRUNE_PROBE_PEERS not in {a.id for a in description.actions}
    assert vpn_protocol.ACTION_PRUNE_PROBE_PEERS not in description.capabilities
    result = await svc.run_command(vpn_protocol.ACTION_PRUNE_PROBE_PEERS, {})
    assert result["dry_run"] is True
