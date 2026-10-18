import json

import pytest

from sa_home_bot.bot.vpn_nodes import ProbeTarget
from sa_home_bot.node import fixups, vpn_probe_state
from sa_home_bot.node.fixups import (
    FixupError,
    build_fixups,
    netfilter,
    privileged,
    probe_net,
    probe_tunnel,
    toolchain,
)
from sa_home_bot.reality import probe_client_config
from sa_home_bot.reality.client_config import RealityParams
from tests.unit.fixups_world import FakeRoot, make_reality_slot, make_settings, make_slot

REALITY_PARAMS = RealityParams(
    endpoint_host="172.245.159.221",
    port=8443,
    server_public_key="pta_VF3o7pvmIw_X3VDePlqIsgOuyWVGrFBvPxjA-wk",
    short_id="51ccffcdddd273ab",
    sni="www.google.com",
)
AWG_PROBE_CONF = """[Interface]
PrivateKey = cHJpdmF0ZS1rZXktZm9yLXRlc3RzLW5vdC1hLXJlYWwtMQ=
Address = 10.9.0.20/32

[Peer]
PublicKey = c2VydmVyLWtleS1vbGQtaW5jYXJuYXRpb24tb2YtamVldmVz
Endpoint = 78.31.250.224:51820
AllowedIPs = 0.0.0.0/0
"""
OLD_SERVER_KEY = "c2VydmVyLWtleS1vbGQtaW5jYXJuYXRpb24tb2YtamVldmVz"
IP = "/usr/bin/ip"


@pytest.fixture
def root(monkeypatch):
    return FakeRoot(monkeypatch)


def _async_return(value):
    async def fake(settings, slot):
        return value

    return fake


def _reality_conf(socks_port: int) -> str:
    return probe_client_config.render_probe_client_config(
        REALITY_PARAMS, "uuid-1", socks_port=socks_port
    )


def _with_slots(monkeypatch, slots):
    monkeypatch.setattr(probe_net, "discover_probe_slots", lambda settings: slots)


def test_probe_slots_get_unique_names_and_transport_specific_fields():
    targets = [
        ProbeTarget(server="jeeves", transport="awg"),
        ProbeTarget(server="jeeves", transport="reality"),
        ProbeTarget(server="wooster", transport="reality"),
    ]
    slots = [probe_net.probe_slot_from_target(i, t) for i, t in enumerate(targets)]
    awg, reality = slots[0], slots[2]
    assert (awg.netns, awg.iface, awg.socks_port) == ("vpn-probe-jeeves-awg", "awg-probe0", None)
    assert (awg.veth_host, awg.veth_ns, awg.subnet) == ("vprobe0h0", "vprobe0n0", "10.200.200.0/30")
    assert (reality.netns, reality.iface, reality.socks_port) == (
        "vpn-probe-wooster-reality",
        None,
        11082,
    )
    for field in ("veth_host", "veth_ns", "subnet", "netns"):
        assert len({getattr(s, field) for s in slots}) == 3
    assert all(len(s.veth_host) <= 15 and len(s.veth_ns) <= 15 for s in slots)


def test_discover_probe_slots_returns_empty_and_warns_on_network_failure(monkeypatch, caplog):
    async def boom(settings):
        raise ConnectionRefusedError("рой недоступен")

    monkeypatch.setattr(probe_net, "_fetch_probe_slots", boom)
    assert probe_net.discover_probe_slots(make_settings(["vpn_check"])) == []
    assert "не удалось обнаружить VPN-серверы" in caplog.text


@pytest.mark.parametrize(
    ("slots", "expected_ids"),
    [
        (
            [make_slot()],
            {
                "vpn-probe-scaffold-jeeves-awg",
                "vpn-probe-conf-jeeves-awg",
                "vpn-check-probe-sudoers",
                "vpn-check-probe-forwarding",
                "vpn-check-probe-forwarding-persist",
                "vpn-check-probe-state",
                "vpn-probe-legacy-cleanup",
            },
        ),
        (
            [make_reality_slot()],
            {
                "vpn-probe-scaffold-wooster-reality",
                "vpn-probe-conf-wooster-reality",
                "vpn-check-probe-sudoers",
                "vpn-check-probe-forwarding",
                "vpn-check-probe-forwarding-persist",
                "vpn-check-probe-state",
                "vpn-probe-legacy-cleanup",
            },
        ),
        (
            [make_slot(server="wooster", transport="openvpn", iface=None)],
            {
                "vpn-check-probe-state",
                "vpn-check-probe-sudoers",
                "vpn-check-probe-forwarding",
                "vpn-check-probe-forwarding-persist",
                "vpn-probe-legacy-cleanup",
            },
        ),
    ],
)
def test_build_fixups_creates_probe_fixups_per_supported_slot(
    root, monkeypatch, slots, expected_ids
):
    _with_slots(monkeypatch, slots)
    ids = [f.id for f in build_fixups(make_settings(["vpn_check"]))]
    assert set(ids) >= expected_ids
    assert ids.index("vpn-probe-legacy-cleanup") < min(
        (i for i, name in enumerate(ids) if name.startswith("vpn-probe-scaffold-")),
        default=len(ids),
    )


def test_build_fixups_without_vpn_check_never_asks_swarm_for_probe_targets(root, monkeypatch):
    discovered = []
    monkeypatch.setattr(probe_net, "discover_probe_slots", lambda s: discovered.append(1) or [])
    ids = {f.id for f in build_fixups(make_settings(["vpn"]))}
    assert not any(i.startswith(("vpn-probe-", "vpn-check-")) for i in ids)
    assert discovered == []


def test_probe_fixups_needed_only_with_vpn_check():
    slot = make_slot()
    fixups_under_test = [
        probe_net.make_vpn_probe_scaffold_fixup(slot),
        probe_net.make_vpn_probe_forwarding_fixup([]),
        probe_net.make_vpn_probe_forwarding_persist_fixup([]),
        probe_net.make_vpn_probe_state_fixup([slot]),
        probe_tunnel.make_vpn_probe_sudoers_fixup([]),
        probe_tunnel.make_vpn_probe_tunnel_conf_fixup(make_settings(["vpn_check"]), slot),
    ]
    for fixup in fixups_under_test:
        assert fixup.needed(make_settings(["vpn_check"]))
        assert not fixup.needed(make_settings(["vpn"]))
    assert fixups_under_test[-1].id == "vpn-probe-conf-jeeves-awg"
    assert fixups_under_test[3].id == "vpn-check-probe-state"


def test_scaffold_unit_has_no_permanent_tunnel_and_idempotent_pre_steps():
    content = probe_net.vpn_probe_scaffold_unit_content(make_slot(), "/usr/sbin/ip")
    assert "awg-quick" not in content
    assert "ExecStart=/bin/true\n" in content
    assert "RemainAfterExit=yes" in content
    assert "ExecStartPre=-/usr/sbin/ip netns add vpn-probe-jeeves-awg\n" in content
    assert (
        "ExecStartPre=-/usr/sbin/ip link add vprobe0h0 type veth peer name vprobe0n0\n" in content
    )
    assert "ExecStartPre=-/usr/sbin/ip link set vprobe0n0 netns vpn-probe-jeeves-awg\n" in content
    assert "ExecStartPre=/usr/sbin/ip link set vprobe0h0 up\n" in content
    assert (
        "ExecStartPre=-/usr/sbin/ip netns exec vpn-probe-jeeves-awg /usr/sbin/ip route add "
        "default via 10.200.200.1\n" in content
    )


@pytest.mark.parametrize(
    ("unit_content", "active", "expected"),
    [
        ("stale old content\n", True, False),
        (probe_net.vpn_probe_scaffold_unit_content(make_slot(), IP), False, False),
        (probe_net.vpn_probe_scaffold_unit_content(make_slot(), IP), True, True),
    ],
)
def test_scaffold_check_compares_content_and_activity(root, unit_content, active, expected):
    unit_path = probe_net.scaffold_unit_path(make_slot())
    root.files[str(unit_path)] = unit_content
    root.active_units = {unit_path.name} if active else set()
    assert probe_net.make_vpn_probe_scaffold_fixup(make_slot()).check() is expected


def test_scaffold_apply_stops_replaced_unit_then_enables(root):
    unit_path = probe_net.scaffold_unit_path(make_slot())
    root.files[str(unit_path)] = "stale\n"
    probe_net.make_vpn_probe_scaffold_fixup(make_slot()).apply()
    assert ["systemctl", "stop", unit_path.name] in root.sudo_calls
    assert root.installed[str(unit_path)] == probe_net.vpn_probe_scaffold_unit_content(
        make_slot(), IP
    )
    assert root.sudo_calls[-2:] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", unit_path.name],
    ]


@pytest.mark.parametrize(
    ("conf", "expected"),
    [
        (None, False),
        ("[Interface]\nTable = off\nAddress = 10.9.0.14/32\n", False),
        ("[Interface]\nAddress = 10.9.0.14/32\n", True),
    ],
)
def test_awg_conf_check(root, conf, expected):
    if conf is not None:
        root.files[str(probe_tunnel.awg_conf_path(make_slot()))] = conf
    assert probe_tunnel._probe_conf_check(make_slot()) is expected


@pytest.mark.parametrize(
    ("conf", "expected"),
    [
        (None, False),
        (_reality_conf(11081), True),
        (_reality_conf(9999), False),
    ],
)
def test_reality_conf_check_requires_expected_socks_port(root, conf, expected):
    slot = make_reality_slot()
    if conf is not None:
        root.files[str(vpn_probe_state.reality_conf_path(slot))] = conf
    assert probe_tunnel._probe_conf_check(slot) is expected


def test_reality_conf_apply_reuses_existing_file(root, monkeypatch):
    slot = make_reality_slot()
    root.files[str(vpn_probe_state.reality_conf_path(slot))] = _reality_conf(11081)
    monkeypatch.setattr(probe_tunnel, "ensure_xray_binary", lambda: None)

    async def must_not_fetch(settings, slot):
        raise AssertionError("файл уже есть — в сеть ходить не нужно")

    monkeypatch.setattr(probe_tunnel, "_fetch_probe_config", must_not_fetch)
    probe_tunnel._probe_conf_apply(make_settings(["vpn_check"]), slot)
    assert root.sudo_calls == []


def test_reality_conf_apply_fetches_and_installs_private_file(root, monkeypatch):
    slot = make_reality_slot()
    monkeypatch.setattr(probe_tunnel, "ensure_xray_binary", lambda: None)
    monkeypatch.setattr(
        probe_tunnel, "_fetch_probe_config", _async_return(_reality_conf(slot.socks_port))
    )
    probe_tunnel._probe_conf_apply(make_settings(["vpn_check"]), slot)
    install = root.sudo_commands("install")[0]
    assert install[-1] == str(vpn_probe_state.reality_conf_path(slot))
    assert install[1:3] == ["-D", "-m"] and "0600" in install


def test_awg_conf_apply_strips_dns_and_table_from_fetched_config(root, monkeypatch):
    raw = AWG_PROBE_CONF.replace("Address", "DNS = 1.1.1.1\nTable = off\nAddress")
    monkeypatch.setattr(probe_tunnel, "_fetch_probe_config", _async_return(raw))
    probe_tunnel._probe_conf_apply(make_settings(["vpn_check"]), make_slot())
    installed = root.installed[str(probe_tunnel.awg_conf_path(make_slot()))]
    assert "DNS" not in installed and "Table" not in installed
    assert "PrivateKey" in installed


def test_awg_conf_apply_keeps_current_config_of_same_server_incarnation(root, monkeypatch):
    conf_path = probe_tunnel.awg_conf_path(make_slot())
    root.files[str(conf_path)] = AWG_PROBE_CONF.replace("Address", "Table = off\nAddress")
    monkeypatch.setattr(probe_tunnel, "_fetch_server_public_key", _async_return(OLD_SERVER_KEY))
    monkeypatch.setattr(
        probe_tunnel, "_fetch_probe_config", _async_return("не должен запрашиваться")
    )
    probe_tunnel._probe_conf_apply(make_settings(["vpn_check"]), make_slot())
    assert "Table" not in root.installed[str(conf_path)]
    assert "PrivateKey = cHJpdmF0" in root.installed[str(conf_path)]


def test_awg_conf_apply_wraps_swarm_failure(root, monkeypatch):
    async def boom(settings, slot):
        raise ConnectionRefusedError("рой недоступен")

    monkeypatch.setattr(probe_tunnel, "_fetch_probe_config", boom)
    with pytest.raises(FixupError, match="не удалось получить конфиг у vpn@jeeves"):
        probe_tunnel._probe_conf_apply(make_settings(["vpn_check"]), make_slot())


def test_ensure_xray_binary_noop_when_version_matches(root):
    root.outputs["/usr/bin/xray version"] = f"Xray {toolchain.XRAY_VERSION.lstrip('v')} (go)\n"
    toolchain.ensure_xray_binary()
    assert root.sudo_calls == []


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        (
            "[Interface]\nPrivateKey = SECRET\nAddress = 10.9.0.15/32\nDNS = 1.1.1.1\n"
            "\n[Peer]\nPublicKey = PUB\n",
            "DNS",
        ),
        (
            "[Interface]\nPrivateKey = SECRET\nTable = off\nAddress = 10.9.0.15/32\n"
            "\n[Peer]\nPublicKey = PUB\n",
            "Table",
        ),
    ],
)
def test_prepare_probe_conf_strips_dns_and_table_lines(raw, forbidden):
    prepared = probe_tunnel.prepare_probe_conf(raw)
    assert forbidden not in prepared
    assert "PrivateKey = SECRET" in prepared and "[Peer]" in prepared


def test_conf_peer_public_key():
    assert probe_tunnel.conf_peer_public_key(AWG_PROBE_CONF) == OLD_SERVER_KEY
    assert probe_tunnel.conf_peer_public_key("[Interface]\nAddress = 10.9.0.20/32\n") is None


@pytest.mark.parametrize(
    ("server_key", "stale"),
    [("c2VydmVyLWtleS1ORVctamVldmVz", True), (OLD_SERVER_KEY, False), (None, False)],
)
def test_probe_conf_stale_only_when_server_reports_different_key(monkeypatch, server_key, stale):
    monkeypatch.setattr(probe_tunnel, "_fetch_server_public_key", _async_return(server_key))
    result = probe_tunnel.probe_conf_stale(
        make_settings(["vpn_check"]), make_slot(), AWG_PROBE_CONF
    )
    assert result is stale


def test_probe_sudoers_content_awg_slot_is_exact():
    content = probe_tunnel.vpn_probe_sudoers_content(
        [make_slot()], "/usr/sbin/ip", "/usr/local/bin/awg-quick", "/usr/local/bin/xray", "sevboa"
    )
    assert content == (
        "sevboa ALL=(root) NOPASSWD: "
        "/usr/sbin/ip netns exec vpn-probe-jeeves-awg curl *, "
        "/usr/sbin/ip netns exec vpn-probe-jeeves-awg /usr/sbin/ip route get *, "
        "/usr/sbin/ip netns exec vpn-probe-jeeves-awg /usr/local/bin/awg-quick up awg-probe0, "
        "/usr/sbin/ip netns exec vpn-probe-jeeves-awg /usr/local/bin/awg-quick down awg-probe0\n"
    )


def test_probe_sudoers_content_reality_slot_grants_only_timeout_xray_run():
    content = probe_tunnel.vpn_probe_sudoers_content(
        [make_reality_slot()],
        "/usr/sbin/ip",
        "/usr/local/bin/awg-quick",
        "/usr/local/bin/xray",
        "sevboa",
    )
    assert "awg-quick" not in content and "route get" not in content
    assert "vpn-probe-wooster-reality curl *" in content
    assert (
        "/usr/sbin/ip netns exec vpn-probe-wooster-reality timeout * /usr/local/bin/xray run -c "
        in content
    )


def test_probe_sudoers_content_joins_slots_with_commas():
    slots = [
        make_slot(),
        make_slot(server="wooster", netns="vpn-probe-wooster-awg", iface="awg-probe1"),
    ]
    content = probe_tunnel.vpn_probe_sudoers_content(slots, "/usr/sbin/ip", "/q", "/x", "sevboa")
    assert content.count("curl *") == 2 and content.count("/q up") == 2


def test_probe_sudoers_check_compares_content(root):
    slots = [make_slot()]
    path = "/etc/sudoers.d/50-sa-home-node-vpn-probe"
    assert probe_tunnel.make_vpn_probe_sudoers_fixup(slots).check() is False
    expected = probe_tunnel.vpn_probe_sudoers_content(
        slots, IP, "/usr/bin/awg-quick", "", __import__("getpass").getuser()
    )
    root.files[path] = expected
    assert probe_tunnel.make_vpn_probe_sudoers_fixup(slots).check() is True
    root.files[path] = "stale\n"
    assert probe_tunnel.make_vpn_probe_sudoers_fixup(slots).check() is False


def test_probe_sudoers_apply_requires_conf_fixup_first(root):
    root.missing_tools = {"awg-quick"}
    with pytest.raises(FixupError, match="awg-quick не найден"):
        probe_tunnel.make_vpn_probe_sudoers_fixup([make_slot()]).apply()


@pytest.mark.parametrize(
    ("ip_forward", "ruleset", "expected"),
    [
        ("1\n", "", True),
        ("1\n", 'iifname "vprobe0h0" accept\nip saddr 10.200.200.0/30 masquerade\n', True),
        ("1\n", "", False),
        ("0\n", 'iifname "vprobe0h0" accept\nip saddr 10.200.200.0/30 masquerade\n', False),
    ],
)
def test_forwarding_check_needs_ip_forward_and_rules_for_every_slot(
    root, ip_forward, ruleset, expected
):
    root.files["/proc/sys/net/ipv4/ip_forward"] = ip_forward
    root.outputs["nft list ruleset"] = ruleset
    slots = [] if expected and not ruleset else [make_slot()]
    assert probe_net.make_vpn_probe_forwarding_fixup(slots).check() is expected


def test_forwarding_apply_installs_nftables_and_own_table_when_missing(root, monkeypatch):
    root.missing_tools = {"nft"}
    root.files["/proc/sys/net/ipv4/ip_forward"] = "1\n"
    monkeypatch.setattr(privileged, "install_argv", lambda pkg: ["apt-get", "install", "-y", pkg])
    probe_net.make_vpn_probe_forwarding_fixup([make_slot()]).apply()
    assert root.sudo_calls[0] == ["apt-get", "install", "-y", "nftables"]
    assert ["nft", "add", "table", "inet", probe_net.VPN_PROBE_NFT_TABLE] in root.sudo_calls
    assert ["nft", "add", "table", "ip", probe_net.VPN_PROBE_NFT_TABLE] in root.sudo_calls


def test_forwarding_apply_skips_install_when_nft_present(root):
    root.files["/proc/sys/net/ipv4/ip_forward"] = "1\n"
    probe_net.make_vpn_probe_forwarding_fixup([make_slot()]).apply()
    assert not root.sudo_commands("apt-get")


def test_forwarding_apply_does_not_duplicate_rules_of_configured_slot(root):
    root.files["/proc/sys/net/ipv4/ip_forward"] = "1\n"
    table = probe_net.VPN_PROBE_NFT_TABLE
    root.outputs[f"nft list chain inet {table} forward"] = (
        "iifname vprobe0h0 accept\noifname vprobe0h0 accept\n"
    )
    root.outputs[f"nft list chain ip {table} postrouting"] = "ip saddr 10.200.200.0/30 masquerade\n"
    new = make_slot(
        server="wooster",
        netns="vpn-probe-wooster-awg",
        veth_host="vprobe1h0",
        veth_ns="vprobe1n0",
        subnet="10.200.200.4/30",
        iface="awg-probe1",
    )
    probe_net.make_vpn_probe_forwarding_fixup([make_slot(), new]).apply()
    inserted = [c for c in root.sudo_calls if c[:3] == ["nft", "insert", "rule"]]
    assert inserted
    assert not any("vprobe0h0" in c or "10.200.200.0/30" in c for c in inserted)
    assert any("vprobe1h0" in c for c in inserted)


def test_forward_script_creates_own_table_and_rules_for_every_slot():
    own_forward = netfilter.NftChain("inet", probe_net.VPN_PROBE_NFT_TABLE, "forward")
    own_nat = netfilter.NftChain("ip", probe_net.VPN_PROBE_NFT_TABLE, "postrouting")
    slots = [
        make_slot(),
        make_slot(server="wooster", veth_host="vprobe1h0", subnet="10.200.200.4/30"),
    ]
    content = probe_net.vpn_probe_forward_script_content(own_forward, own_nat, slots)
    assert content.startswith("#!/bin/sh\n") and "set -e" in content
    assert f"nft list table inet {probe_net.VPN_PROBE_NFT_TABLE}" in content
    assert "type filter hook forward" in content and "type nat hook postrouting" in content
    for slot in slots:
        assert f'iifname "{slot.veth_host}" accept' in content
        assert f'oifname "{slot.veth_host}" accept' in content
        assert f"ip saddr {slot.subnet} masquerade" in content


def test_forward_script_waits_for_foreign_tables_instead_of_creating_them():
    forward = netfilter.NftChain("inet", "filter", "forward")
    nat = netfilter.NftChain("ip", "nat", "postrouting")
    content = probe_net.vpn_probe_forward_script_content(forward, nat, [make_slot()])
    assert "nft add table" not in content
    assert "nft list chain inet filter forward" in content
    assert "nft list chain ip nat postrouting" in content
    assert "wait_table inet filter" in content and "wait_table ip nat" in content


def test_forward_unit_runs_after_scaffolds_and_tailscaled_and_retries():
    slots = [
        make_slot(),
        make_slot(server="wooster", netns="vpn-probe-wooster-awg", iface="awg-probe1"),
    ]
    content = probe_net.vpn_probe_forward_unit_content(
        probe_net.VPN_PROBE_FORWARD_SCRIPT_PATH, slots
    )
    for slot in slots:
        assert probe_net.scaffold_unit_path(slot).name in content
    assert f"ExecStart=/bin/sh {probe_net.VPN_PROBE_FORWARD_SCRIPT_PATH}\n" in content
    for required in (
        "Type=oneshot",
        "RemainAfterExit=yes",
        "WantedBy=multi-user.target",
        "tailscaled.service",
        "Restart=on-failure",
    ):
        assert required in content
    empty = probe_net.vpn_probe_forward_unit_content(probe_net.VPN_PROBE_FORWARD_SCRIPT_PATH, [])
    assert "After=network-online.target tailscaled.service\n" in empty


def test_forwarding_persist_check(root):
    slots = [make_slot()]
    fixup = probe_net.make_vpn_probe_forwarding_persist_fixup(slots)
    assert fixup.check() is False
    script, unit = probe_net.VPN_PROBE_FORWARD_SCRIPT_PATH, probe_net.VPN_PROBE_FORWARD_UNIT_FILE
    root.files[str(script)] = "script"
    root.files[str(unit)] = "old content\n"
    root.active_units = {unit.name}
    assert fixup.check() is False
    root.files[str(unit)] = probe_net.vpn_probe_forward_unit_content(script, slots)
    assert fixup.check() is True
    root.active_units = set()
    assert fixup.check() is False


def test_forwarding_persist_apply_bakes_resolved_chains_and_enables_unit(root):
    root.files["/proc/sys/net/ipv4/ip_forward"] = "1\n"
    root.outputs["nft -j list ruleset"] = json.dumps(
        {
            "nftables": [
                {
                    "chain": {
                        "hook": "forward",
                        "type": "filter",
                        "family": "inet",
                        "table": "filter",
                        "name": "forward",
                    }
                },
                {
                    "chain": {
                        "hook": "postrouting",
                        "type": "nat",
                        "family": "ip",
                        "table": "nat",
                        "name": "postrouting",
                    }
                },
            ]
        }
    )
    probe_net.make_vpn_probe_forwarding_persist_fixup([make_slot()]).apply()
    script = root.installed[str(probe_net.VPN_PROBE_FORWARD_SCRIPT_PATH)]
    assert "wait_table inet filter" in script
    assert root.sudo_commands("install")[0][1] == "-D"
    unit = probe_net.VPN_PROBE_FORWARD_UNIT_FILE
    assert ["systemctl", "daemon-reload"] in root.sudo_calls
    assert ["systemctl", "enable", "--now", unit.name] in root.sudo_calls


def test_state_fixup_checks_and_installs_rendered_json(root):
    slots = [make_slot()]
    fixup = probe_net.make_vpn_probe_state_fixup(slots)
    assert fixup.check() is False
    fixup.apply()
    assert vpn_probe_state.parse(root.installed[str(vpn_probe_state.STATE_PATH)]) == slots
    assert fixup.check() is True


@pytest.mark.parametrize("legacy_present", [False, True])
def test_legacy_cleanup_fixup_demands_work_only_while_legacy_alive(root, legacy_present):
    root.outputs["ip link show vprobe-veth0"] = None
    if legacy_present:
        root.files[str(probe_net.VPN_PROBE_SCAFFOLD_UNIT_DIR / probe_net.VPN_PROBE_LEGACY_UNIT)] = (
            "x"
        )
    assert probe_net.make_vpn_probe_legacy_cleanup_fixup().check() is not legacy_present


def test_fixups_package_keeps_external_api():
    assert fixups.build_fixups is build_fixups
    assert fixups.SMARTCTL_WRAPPER_PATH.name == "smartctl"
