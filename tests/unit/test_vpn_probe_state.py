from __future__ import annotations

import pytest

from sa_home_bot.node import vpn_probe_state as state


def _slot(**overrides) -> state.ProbeSlot:
    base = dict(
        server="jeeves",
        transport="awg",
        netns="vpn-probe-jeeves-awg",
        veth_host="vprobe0h0",
        veth_ns="vprobe0n0",
        veth_host_addr="10.200.200.1/30",
        veth_ns_addr="10.200.200.2/30",
        subnet="10.200.200.0/30",
        iface="awg-probe0",
    )
    base.update(overrides)
    return state.ProbeSlot(**base)


def test_load_missing_file_returns_empty(tmp_path):
    assert state.load(tmp_path / "does-not-exist.json") == []


def test_render_parse_round_trip():
    slots = [
        _slot(),
        _slot(
            server="wooster",
            transport="reality",
            netns="vpn-probe-wooster-reality",
            veth_host="vprobe1h0",
            veth_ns="vprobe1n0",
            veth_host_addr="10.200.200.5/30",
            veth_ns_addr="10.200.200.6/30",
            subnet="10.200.200.4/30",
            iface=None,
            socks_port=11081,
        ),
    ]
    parsed = state.parse(state.render(slots))
    assert parsed == slots


def test_load_reads_file_written_via_render(tmp_path):
    path = tmp_path / "vpn-probe-state.json"
    path.write_text(state.render([_slot()]))
    assert state.load(path) == [_slot()]


@pytest.mark.parametrize("sni", [None, "www.azgez.com"])
def test_sni_round_trips_through_state(sni):
    slots = [_slot(transport="reality", iface=None, socks_port=11081, sni=sni)]
    assert state.parse(state.render(slots)) == slots
    assert state.parse(state.render(slots))[0].sni == sni


def test_old_state_without_sni_is_read():
    old = (
        '{"slots": [{"server": "jeeves", "transport": "reality", "netns": "n", '
        '"veth_host": "a", "veth_ns": "b", "veth_host_addr": "10.0.0.1/30", '
        '"veth_ns_addr": "10.0.0.2/30", "subnet": "10.0.0.0/30"}]}'
    )
    assert state.parse(old)[0].sni is None
