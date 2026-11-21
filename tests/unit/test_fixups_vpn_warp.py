from __future__ import annotations

import base64
import copy
import json
import stat

import pytest
from pydantic import ValidationError

from sa_home_bot.config import WARP_DEFAULT_DOMAINS, RealityTransportConfig, VpnWarpConfig
from sa_home_bot.node.fixups import FixupError, vpn_server, vpn_warp
from tests.unit.fixups_world import make_settings

DOMAINS = ["youtube.com", "googlevideo.com"]
INBOUNDS = ["reality-in", "reality-alt"]


def _key(n: int = 1) -> dict:
    return {
        "secretKey": f"sk{n}",
        "address": [f"172.16.0.{n}/32", f"fd01::{n}/128"],
        "peerPublicKey": "peerpub",
        "endpoint": "162.159.192.1:2408",
        "reserved": [1, 2, 3],
    }


def _config() -> dict:
    sniffing = {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": True}
    config = {
        "inbounds": [
            {"tag": "reality-in", "sniffing": copy.deepcopy(sniffing)},
            {"tag": "api-in"},
            {"tag": "reality-alt", "sniffing": copy.deepcopy(sniffing)},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
        "routing": {
            "rules": [{"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"}],
        },
    }
    return vpn_server.with_private_block(config)


def _applied(n: int = 2) -> dict:
    return vpn_warp.with_warp(_config(), [_key(i) for i in range(1, n + 1)], DOMAINS)


def test_with_warp_idempotent():
    once = _applied()
    twice = vpn_warp.with_warp(once, [_key(1), _key(2)], DOMAINS)
    assert twice == once
    assert [o["tag"] for o in once["outbounds"]].count("warp-1") == 1
    assert len(once["routing"]["balancers"]) == 1
    assert sum(1 for r in once["routing"]["rules"] if r.get("balancerTag") == "warp") == 1


def test_with_warp_rule_after_private_block_and_balancer():
    rules = _applied()["routing"]["rules"]
    index = next(i for i, r in enumerate(rules) if r.get("balancerTag") == "warp")
    block_indexes = [i for i, r in enumerate(rules) if r.get("outboundTag") == "block"]
    assert index > max(block_indexes)
    assert rules[index]["inboundTag"] == INBOUNDS
    assert rules[index]["domain"] == ["domain:youtube.com", "domain:googlevideo.com"]
    assert "outboundTag" not in rules[index]


def test_with_warp_balancer_and_observatory():
    out = _applied()
    assert out["routing"]["balancers"] == [
        {"tag": "warp", "selector": ["warp-"], "strategy": {"type": "leastPing"}}
    ]
    assert out["burstObservatory"]["subjectSelector"] == ["warp-"]
    ping = out["burstObservatory"]["pingConfig"]
    assert ping["destination"] == "https://www.google.com/generate_204"
    assert ping["interval"] == "5m"
    assert ping["timeout"] == "10s"


def test_with_warp_adds_quic_to_sniffing_without_duplicates():
    out = _applied()
    again = vpn_warp.with_warp(out, [_key(1), _key(2)], DOMAINS)
    for inbound in again["inbounds"]:
        if inbound["tag"] in INBOUNDS:
            assert inbound["sniffing"]["destOverride"] == ["http", "tls", "quic"]
    assert "sniffing" not in again["inbounds"][1]


def test_with_warp_replaces_manual_outbound_tag():
    config = _config()
    config["outbounds"].append({"tag": "warp", "protocol": "wireguard", "settings": {}})
    config["routing"]["rules"].append(
        {"type": "field", "inboundTag": INBOUNDS, "domain": ["domain:x.com"], "outboundTag": "warp"}
    )
    out = vpn_warp.with_warp(config, [_key(1)], DOMAINS)
    assert [o["tag"] for o in out["outbounds"] if o["tag"].startswith("warp")] == ["warp-1"]
    warp_rules = [
        r for r in out["routing"]["rules"] if "warp" in (r.get("outboundTag"), r.get("balancerTag"))
    ]
    assert len(warp_rules) == 1


def test_warp_outbound_shape():
    outbound = vpn_warp.warp_outbound(_key(1), "warp-1")
    assert outbound["protocol"] == "wireguard"
    assert outbound["settings"]["peers"] == [
        {"publicKey": "peerpub", "endpoint": "162.159.192.1:2408"}
    ]
    assert outbound["settings"]["reserved"] == [1, 2, 3]
    assert outbound["settings"]["mtu"] == 1280
    assert outbound["settings"]["domainStrategy"] == "ForceIPv4"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: None, True),
        (lambda c: c["outbounds"].pop(), False),
        (lambda c: c["routing"].update(balancers=[]), False),
        (lambda c: c.pop("burstObservatory"), False),
        (lambda c: c["inbounds"][0]["sniffing"]["destOverride"].remove("quic"), False),
        (lambda c: c["routing"]["rules"].reverse(), False),
        (lambda c: c["routing"]["rules"][-1].update(domain=["domain:other.com"]), False),
    ],
)
def test_warp_applied(mutate, expected):
    config = _applied()
    mutate(config)
    assert vpn_warp.warp_applied(config, 2, DOMAINS) is expected


def test_warp_applied_wrong_key_count():
    assert vpn_warp.warp_applied(_applied(2), 3, DOMAINS) is False
    assert vpn_warp.warp_applied(_config(), 1, DOMAINS) is False


def _registration(client_id: bytes = b"\x01\x02\x03") -> dict:
    return {
        "config": {
            "interface": {"addresses": {"v4": "172.16.0.2", "v6": "2606:4700::1"}},
            "peers": [
                {"public_key": "peerpub", "endpoint": {"v4": "162.159.192.7:0", "host": "x"}}
            ],
            "client_id": base64.b64encode(client_id).decode(),
        }
    }


def test_parse_warp_registration():
    key = vpn_warp.parse_warp_registration(_registration(), "priv")
    assert key == {
        "secretKey": "priv",
        "address": ["172.16.0.2/32", "2606:4700::1/128"],
        "peerPublicKey": "peerpub",
        "endpoint": "162.159.192.7:2408",
        "reserved": [1, 2, 3],
    }


@pytest.mark.parametrize("resp", [{}, {"config": {}}, _registration(b"\x01\x02")])
def test_parse_warp_registration_rejects_incomplete(resp):
    with pytest.raises(FixupError):
        vpn_warp.parse_warp_registration(resp, "priv")


def test_register_warp_key_request_shape():
    seen = {}

    def post(url, payload, headers):
        seen.update(url=url, payload=payload, headers=headers)
        return _registration()

    key = vpn_warp.register_warp_key(post=post, keygen=lambda: ("priv", "pub"))
    assert key["secretKey"] == "priv"
    assert seen["url"] == "https://api.cloudflareclient.com/v0a2158/reg"
    assert seen["payload"]["key"] == "pub"
    assert seen["payload"]["type"] == "Android"
    assert seen["payload"]["tos"].endswith("Z")
    assert len(seen["payload"]["tos"].split(".")[1]) == 4
    assert seen["headers"]["User-Agent"] == "okhttp/3.12.1"
    assert seen["headers"]["CF-Client-Version"] == "a-6.10-2158"


def test_register_warp_key_wraps_network_error():
    def post(url, payload, headers):
        raise OSError("net")

    with pytest.raises(FixupError):
        vpn_warp.register_warp_key(post=post, keygen=lambda: ("priv", "pub"))


@pytest.fixture
def xray_dir(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config()))
    monkeypatch.setattr(vpn_warp, "xray_config_path", lambda: config_path)
    return tmp_path


def test_ensure_warp_keys_adopts_legacy_file(xray_dir):
    legacy = xray_dir / "warp.json"
    legacy.write_text(json.dumps(_key(9)))
    calls = []

    def register():
        calls.append(1)
        return _key(2)

    keys = vpn_warp.ensure_warp_keys(2, register=register)
    assert keys[0]["secretKey"] == "sk9"
    assert not legacy.exists()
    assert (xray_dir / "warp-1.json").exists()
    assert len(calls) == 1
    assert keys[1]["secretKey"] == "sk2"


def test_ensure_warp_keys_registers_only_missing(xray_dir):
    (xray_dir / "warp-1.json").write_text(json.dumps(_key(1)))
    calls = []

    def register():
        calls.append(1)
        return _key(5)

    vpn_warp.ensure_warp_keys(3, register=register)
    assert len(calls) == 2
    assert stat.S_IMODE((xray_dir / "warp-3.json").stat().st_mode) == 0o600
    vpn_warp.ensure_warp_keys(3, register=register)
    assert len(calls) == 2


def _settings(warp: VpnWarpConfig | None, with_reality: bool = True, services=("vpn",)):
    s = make_settings(list(services))
    if with_reality:
        s.vpn.reality = RealityTransportConfig(
            endpoint_host="1.2.3.4",
            port=8443,
            server_public_key="pub",
            short_id="sid",
            sni="www.google.com",
        )
    s.vpn.warp = warp
    return s


@pytest.mark.parametrize(
    ("warp", "with_reality", "services", "expected"),
    [
        (VpnWarpConfig(), True, ("vpn",), True),
        (None, True, ("vpn",), False),
        (VpnWarpConfig(), False, ("vpn",), False),
        (VpnWarpConfig(), True, ("monitor",), False),
    ],
)
def test_fixup_needed(warp, with_reality, services, expected):
    s = _settings(warp, with_reality, services)
    assert vpn_warp.make_vpn_warp_fixup(s).needed(s) is expected


def test_fixup_check_and_apply(xray_dir, monkeypatch):
    s = _settings(VpnWarpConfig(keys=2, domains=DOMAINS))
    fixup = vpn_warp.make_vpn_warp_fixup(s)
    written = {}
    monkeypatch.setattr(
        vpn_warp, "write_xray_config_with_test", lambda path, cfg: written.update(cfg=cfg)
    )
    monkeypatch.setattr(vpn_warp, "register_warp_key", lambda: _key(7))
    ok = type("Done", (), {"returncode": 0})()
    monkeypatch.setattr(vpn_warp.subprocess, "run", lambda *a, **k: ok)
    assert fixup.check() is False
    fixup.apply()
    assert (xray_dir / "warp-1.json").exists()
    assert (xray_dir / "warp-2.json").exists()
    assert vpn_warp.warp_applied(written["cfg"], 2, DOMAINS)
    (xray_dir / "config.json").write_text(json.dumps(written["cfg"]))
    assert fixup.check() is True


def test_fixup_registered_in_build():
    from sa_home_bot.node.fixups import build_fixups

    s = _settings(VpnWarpConfig())
    assert "vpn-warp" in [f.id for f in build_fixups(s)]
    s_off = _settings(None)
    assert "vpn-warp" not in [f.id for f in build_fixups(s_off)]


def test_warp_config_defaults_and_bounds():
    cfg = VpnWarpConfig()
    assert cfg.keys == 2
    assert tuple(cfg.domains) == WARP_DEFAULT_DOMAINS
    assert len(cfg.domains) == 18
    assert VpnWarpConfig(keys=4, domains=["a.com"]).domains == ["a.com"]
    for bad in (0, 5):
        with pytest.raises(ValidationError):
            VpnWarpConfig(keys=bad)
