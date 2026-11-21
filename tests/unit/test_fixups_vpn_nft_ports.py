from __future__ import annotations

import json
from pathlib import Path

import pytest

from sa_home_bot.config import (
    AwgAltConfig,
    RealityAltConfig,
    RealityTransportConfig,
    RealityXhttpConfig,
)
from sa_home_bot.node.fixups import FixupError, vpn_server
from tests.unit.fixups_world import FakeRoot, make_settings

JEEVES_CONF = (Path(__file__).parent / "support" / "jeeves_nftables.conf").read_text()
CONF_PATH = "/etc/nftables.conf"
LIVE_KEY = "nft -j list table inet filter"
JEEVES_SPECS = [
    "51820/udp",
    "8443/udp",
    "8443/tcp",
    "2083/tcp",
    "2087/tcp",
    "8444/tcp",
    "80/tcp",
]
MINIMAL_CONF = (
    "table inet filter {\n"
    "\tchain input {\n"
    "\t\ttype filter hook input priority filter; policy drop;\n"
    "\t\tct state established,related accept\n"
    "\t}\n"
    "}\n"
)


def _jeeves_settings():
    s = make_settings(["vpn"])
    s.vpn.endpoint_port = 51820
    s.vpn.sub_port = 8444
    s.vpn.awg_alt = AwgAltConfig(port=8443)
    s.vpn.reality = RealityTransportConfig(
        endpoint_host="h",
        port=8443,
        server_public_key="p",
        short_id="s",
        alt=RealityAltConfig(port=2083, sni="a.example"),
        xhttp=RealityXhttpConfig(port=2087, sni="b.example", path="/ring"),
    )
    return s


def _live(*opened):
    rules = []
    for proto, port in opened:
        expr = [
            {
                "match": {
                    "op": "==",
                    "left": {"payload": {"protocol": proto, "field": "dport"}},
                    "right": port,
                }
            },
            {"accept": None},
        ]
        rules.append({"rule": {"family": "inet", "chain": "input", "expr": expr}})
    return json.dumps({"nftables": rules})


def _all_live():
    return _live(*[(p.split("/")[1], int(p.split("/")[0])) for p in JEEVES_SPECS])


@pytest.fixture
def root(monkeypatch):
    world = FakeRoot(monkeypatch)
    monkeypatch.setattr(vpn_server, "ufw_in_use", lambda: False)
    return world


def test_specs_for_jeeves_config():
    assert vpn_server.vpn_server_port_specs(_jeeves_settings()) == JEEVES_SPECS


def test_specs_minimal_config():
    s = make_settings(["vpn"])
    assert vpn_server.vpn_server_port_specs(s) == [f"{s.vpn.endpoint_port}/udp"]


def test_real_jeeves_conf_unchanged_for_current_ports():
    assert vpn_server.nftables_conf_with_ports(JEEVES_CONF, JEEVES_SPECS) == JEEVES_CONF


def test_real_jeeves_conf_new_port_adds_exactly_one_line():
    out = vpn_server.nftables_conf_with_ports(JEEVES_CONF, [*JEEVES_SPECS, "2095/tcp"])
    old = JEEVES_CONF.splitlines()
    added = [line for line in out.splitlines() if line not in old]
    assert added == ["\t\ttcp dport 2095 accept\t# sa-home vpn (nodectl fix)"]
    assert len(out.splitlines()) == len(old) + 1


@pytest.mark.parametrize("specs", [["2087/tcp", "51820/udp"], ["51820/udp"]])
def test_conf_with_ports_idempotent(specs):
    once = vpn_server.nftables_conf_with_ports(MINIMAL_CONF, specs)
    assert vpn_server.nftables_conf_with_ports(once, specs) == once


@pytest.mark.parametrize(
    "existing",
    [
        "tcp dport 2087 accept",
        "tcp dport 2087 counter name mtg_bytes accept",
        "tcp dport 2087 accept\t# note",
    ],
)
def test_existing_rule_counts_as_open(existing):
    content = MINIMAL_CONF.replace("\t\tct state", f"\t\t{existing}\n\t\tct state")
    assert vpn_server.nftables_conf_with_ports(content, ["2087/tcp"]) == content


@pytest.mark.parametrize(
    "existing",
    [
        'iifname "tailscale0" tcp dport 2087 accept',
        "udp dport 2087 accept",
        "tcp dport 2087 drop",
    ],
)
def test_non_matching_rule_does_not_count(existing):
    content = MINIMAL_CONF.replace("\t\tct state", f"\t\t{existing}\n\t\tct state")
    out = vpn_server.nftables_conf_with_ports(content, ["2087/tcp"])
    assert "tcp dport 2087 accept\t# sa-home" in out


def test_rule_in_other_chain_does_not_count():
    content = MINIMAL_CONF.replace(
        "\t}\n}\n", "\t}\n\tchain forward {\n\t\ttcp dport 2087 accept\n\t}\n}\n"
    )
    assert "sa-home vpn" in vpn_server.nftables_conf_with_ports(content, ["2087/tcp"])


def test_insert_order_after_hook_line_in_spec_order():
    out = vpn_server.nftables_conf_with_ports(MINIMAL_CONF, ["2087/tcp", "51820/udp"])
    lines = out.splitlines()
    hook = next(i for i, s in enumerate(lines) if "hook input" in s)
    assert lines[hook + 1].lstrip().startswith("tcp dport 2087 accept")
    assert lines[hook + 2].lstrip().startswith("udp dport 51820 accept")
    assert lines[hook + 3].lstrip().startswith("ct state")
    assert lines[hook + 1].startswith("\t\t")


@pytest.mark.parametrize("content", ["table ip nat {\n}\n", "", "table inet filter {\n}\n"])
def test_foreign_structure_raises(content):
    with pytest.raises(FixupError, match="структура файла не та"):
        vpn_server.nftables_conf_with_ports(content, ["2087/tcp"])


@pytest.mark.parametrize(
    ("ufw", "content", "needed"),
    [
        (False, MINIMAL_CONF, True),
        (True, MINIMAL_CONF, False),
        (False, "table ip nat {\n}\n", False),
        (False, None, False),
    ],
)
def test_needed(root, monkeypatch, ufw, content, needed):
    monkeypatch.setattr(vpn_server, "ufw_in_use", lambda: ufw)
    if content is not None:
        root.files[CONF_PATH] = content
    s = _jeeves_settings()
    assert vpn_server.make_vpn_nft_ports_fixup(s).needed(s) is needed


def test_needed_false_without_awg(root):
    root.files[CONF_PATH] = MINIMAL_CONF
    s = make_settings(["monitor"])
    assert vpn_server.make_vpn_nft_ports_fixup(s).needed(s) is False


@pytest.mark.parametrize(
    ("conf", "live", "ok"),
    [
        (JEEVES_CONF, "all", True),
        (JEEVES_CONF, "none", False),
        (MINIMAL_CONF, "all", False),
    ],
)
def test_check_needs_file_and_live(root, conf, live, ok):
    root.files[CONF_PATH] = conf
    root.outputs[LIVE_KEY] = _all_live() if live == "all" else _live()
    s = _jeeves_settings()
    assert vpn_server.make_vpn_nft_ports_fixup(s).check() is ok


def test_apply_inserts_only_missing_live_rules_and_never_reloads(root):
    root.files[CONF_PATH] = JEEVES_CONF
    root.outputs[LIVE_KEY] = _live(("udp", 51820), ("tcp", 8443), ("tcp", 80))
    s = _jeeves_settings()
    vpn_server.make_vpn_nft_ports_fixup(s).apply()
    inserts = [c for c in root.sudo_calls if c[:2] == ["nft", "insert"]]
    assert [c[-4:-1] for c in inserts] == [
        ["udp", "dport", "8443"],
        ["tcp", "dport", "2083"],
        ["tcp", "dport", "2087"],
        ["tcp", "dport", "8444"],
    ]
    assert all(c[2:6] == ["rule", "inet", "filter", "input"] for c in inserts)
    assert all(c[-1] == "accept" for c in inserts)
    assert not [c for c in root.sudo_calls if "-f" in c and "-c" not in c]
    assert not [c for c in root.sudo_calls if c[0] == "systemctl"]
    assert CONF_PATH not in root.installed


def test_apply_updates_file_via_check_then_install(root):
    root.files[CONF_PATH] = MINIMAL_CONF
    root.outputs[LIVE_KEY] = _all_live()
    s = _jeeves_settings()
    vpn_server.make_vpn_nft_ports_fixup(s).apply()
    assert any(c[1:3] == ["-c", "-f"] for c in root.sudo_calls)
    assert "udp dport 51820 accept" in root.installed[CONF_PATH]
    assert not [c for c in root.sudo_calls if c[:2] == ["nft", "insert"]]
