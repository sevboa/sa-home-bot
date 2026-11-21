from __future__ import annotations

import json

import pytest

from sa_home_bot.bot.vpn_nodes import ProbeTarget
from sa_home_bot.node import vpn_probe_state
from sa_home_bot.node.fixups import probe_net
from tests.unit.fixups_world import FakeRoot, make_slot

TRANSPORTS = ("awg", "reality", "reality_alt", "awg_alt", "reality_xhttp")
UNIT_LIST = "systemctl list-unit-files sa-home-vpn-probe-scaffold-*.service --no-legend --plain"
NETNS_LIST = "ip netns list"
VETH_LIST = "ip -o link show type veth"
JEEVES_UNIT = "sa-home-vpn-probe-scaffold-jeeves-awg.service"
GONE_UNIT = "sa-home-vpn-probe-scaffold-gone-reality.service"


@pytest.fixture
def root(monkeypatch):
    return FakeRoot(monkeypatch)


def _targets(servers, transports=("awg", "reality")):
    return [ProbeTarget(server=s, transport=t) for s in servers for t in transports]


def _by_key(slots):
    return {vpn_probe_state.slot_key(s.server, s.transport): s for s in slots}


def _plan(targets, known):
    indices = probe_net.assign_indices(targets, known)
    return probe_net.ProbePlan(
        slots=probe_net.probe_slots_from_targets(targets, indices), indices=indices
    )


@pytest.mark.parametrize("new_server", ["ion", "aaa", "zzz"])
def test_new_server_does_not_shift_existing_slots(new_server):
    before = _plan(_targets(["jeeves", "wooster"], TRANSPORTS), {})
    after = _plan(_targets(["jeeves", "wooster", new_server], TRANSPORTS), before.indices)
    before_slots, after_slots = _by_key(before.slots), _by_key(after.slots)
    for key, slot in before_slots.items():
        assert after_slots[key] == slot
    assert len(set(after_slots) - set(before_slots)) == len(TRANSPORTS)
    assert len(set(after.indices.values())) == len(after.indices)


@pytest.mark.parametrize("missing", ["jeeves", "ion"])
def test_vanished_and_returned_server_keeps_indices(missing):
    servers = ["ion", "jeeves", "wooster"]
    visible = [s for s in servers if s != missing]
    full = _plan(_targets(servers, TRANSPORTS), {})
    degraded = _plan(_targets(visible, TRANSPORTS), full.indices)
    assert degraded.indices == full.indices
    newcomer = _plan(_targets(["aaa", *visible], TRANSPORTS), degraded.indices)
    assert all(newcomer.indices[k] == v for k, v in full.indices.items())
    returned = _plan(_targets(servers, TRANSPORTS), newcomer.indices)
    assert _by_key(returned.slots) == _by_key(full.slots)


def test_no_collisions_for_sixteen_servers_and_five_transports():
    servers = [f"srv{i:02d}" for i in range(16)]
    indices: dict[str, int] = {}
    for count in range(1, 17):
        plan = _plan(_targets(reversed(servers[:count]), TRANSPORTS), indices)
        indices = plan.indices
    slots = plan.slots
    assert len(slots) == 80
    for field in ("veth_host", "veth_ns", "subnet", "netns", "veth_host_addr", "veth_ns_addr"):
        assert len({getattr(s, field) for s in slots}) == 80
    assert len({s.iface for s in slots if s.iface}) == 32
    assert len({s.socks_port for s in slots if s.socks_port}) == 48
    assert all(len(s.veth_host) <= 15 and len(s.veth_ns) <= 15 for s in slots)
    assert all(len(s.iface) <= 15 for s in slots if s.iface)
    assert all(s.subnet.startswith(("10.200.200.", "10.200.201.")) for s in slots)


def test_old_state_without_indices_is_accepted(root):
    old = [
        make_slot(server="wooster", transport="reality", veth_host="vprobe5h0"),
        make_slot(server="jeeves", transport="awg", veth_host="vprobe2h0"),
    ]
    root.files[str(vpn_probe_state.STATE_PATH)] = json.dumps(
        {"slots": [s.model_dump() for s in old]}
    )
    known = vpn_probe_state.load_indices()
    assert known == {"wooster/reality": 5, "jeeves/awg": 2}
    plan = _plan(_targets(["ion", "jeeves", "wooster"]), known)
    assert plan.indices == {
        "wooster/reality": 5,
        "jeeves/awg": 2,
        "ion/awg": 0,
        "ion/reality": 1,
        "jeeves/reality": 3,
        "wooster/awg": 4,
    }


def test_unreadable_state_gives_no_known_indices(root):
    root.files[str(vpn_probe_state.STATE_PATH)] = "не json"
    assert vpn_probe_state.load_indices() == {}


def test_state_fixup_persists_indices_of_invisible_server(root):
    indices = {"jeeves/awg": 0, "ion/awg": 7}
    fixup = probe_net.make_vpn_probe_state_fixup([make_slot()], indices)
    assert fixup.check() is False
    fixup.apply()
    assert vpn_probe_state.load_indices() == indices
    assert fixup.check() is True


def _list_host(root, units, netns, veths):
    root.outputs[UNIT_LIST] = "".join(f"{u} enabled enabled\n" for u in units)
    root.outputs[NETNS_LIST] = "".join(f"{n} (id: 1)\n" for n in netns)
    root.outputs[VETH_LIST] = "".join(
        f"{i}: {v}@if3: <BROADCAST> mtu 1500 qdisc noop state DOWN\n"
        for i, v in enumerate(veths, 1)
    )


def test_stale_cleanup_check_sees_only_foreign_slots(root):
    plan = _plan(_targets(["jeeves"]), {"ion/awg": 9})
    own = dict(
        units=[JEEVES_UNIT, "sa-home-vpn-probe-scaffold-ion-awg.service"],
        netns=["vpn-probe-jeeves-awg", "vpn-probe-ion-awg", "unrelated"],
        veths=["vprobe0h0", "vprobe9h0"],
    )
    fixup = probe_net.make_vpn_probe_stale_cleanup_fixup(plan)
    _list_host(root, **own)
    assert fixup.check() is True
    _list_host(root, **{**own, "veths": [*own["veths"], "vprobe44h0"]})
    assert fixup.check() is False
    _list_host(root, **{**own, "netns": [*own["netns"], "vpn-probe-gone-reality"]})
    assert fixup.check() is False
    _list_host(root, **{**own, "units": [*own["units"], GONE_UNIT]})
    assert fixup.check() is False


def test_stale_cleanup_apply_removes_foreign_slots_only(root, monkeypatch):
    plan = _plan(_targets(["jeeves"]), {})
    _list_host(
        root,
        units=[JEEVES_UNIT, GONE_UNIT],
        netns=["vpn-probe-jeeves-awg", "vpn-probe-gone-reality"],
        veths=["vprobe0h0", "vprobe44h0"],
    )
    real_find = probe_net.find_stale_probe_artifacts
    stale = real_find(plan.indices)
    answers = iter([stale, probe_net.StaleProbeArtifacts([], [], [])])
    monkeypatch.setattr(probe_net, "find_stale_probe_artifacts", lambda indices: next(answers))
    probe_net.make_vpn_probe_stale_cleanup_fixup(plan).apply()
    assert stale == probe_net.StaleProbeArtifacts(
        [GONE_UNIT], ["vpn-probe-gone-reality"], ["vprobe44h0"]
    )
    assert [c for c in root.sudo_calls if c != ["ip", "netns", "list"]] == [
        ["systemctl", "disable", "--now", GONE_UNIT],
        ["rm", "-f", f"/etc/systemd/system/{GONE_UNIT}"],
        ["systemctl", "daemon-reload"],
        ["ip", "netns", "del", "vpn-probe-gone-reality"],
        ["ip", "link", "del", "vprobe44h0"],
    ]


def test_stale_cleanup_idle_when_discovery_found_nothing(root):
    _list_host(root, ["sa-home-vpn-probe-scaffold-x-awg.service"], ["vpn-probe-x-awg"], [])
    plan = probe_net.ProbePlan(slots=[], indices={})
    assert probe_net.make_vpn_probe_stale_cleanup_fixup(plan).check() is True
