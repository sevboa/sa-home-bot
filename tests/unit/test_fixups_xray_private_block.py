from __future__ import annotations

import json
import subprocess

import pytest

from sa_home_bot.config import RealityTransportConfig
from sa_home_bot.node.fixups import vpn_server
from sa_home_bot.node.fixups.build import build_fixups
from tests.unit.fixups_world import FakeRoot, make_settings


def _server_config(*extra_tags: str) -> dict:
    inbounds = [{"tag": "reality-in", "port": 8443}, {"tag": "api-in", "port": 10085}]
    inbounds += [{"tag": tag, "port": 2000 + i} for i, tag in enumerate(extra_tags)]
    return {
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "rules": [{"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"}]
        },
    }


def _settings():
    s = make_settings(["vpn"])
    s.vpn.reality = RealityTransportConfig(
        endpoint_host="1.2.3.4",
        port=8443,
        server_public_key="pub",
        short_id="sid",
        sni="www.google.com",
        api_addr="127.0.0.1:10085",
    )
    return s


def test_old_config_is_not_blocking():
    assert not vpn_server.xray_blocks_private(_server_config("reality-alt"))


def test_with_private_block_covers_every_client_inbound():
    config = vpn_server.with_private_block(_server_config("reality-alt", "reality-xhttp"))
    assert vpn_server.xray_blocks_private(config)
    rules = config["routing"]["rules"]
    assert rules[0]["inboundTag"] == ["api-in"]
    for rule in rules[1:3]:
        assert rule["outboundTag"] == "block"
        assert sorted(rule["inboundTag"]) == ["reality-alt", "reality-in", "reality-xhttp"]
    assert config["routing"]["domainStrategy"] == "IPIfNonMatch"


@pytest.mark.parametrize(
    "dest", ["127.0.0.0/8", "100.64.0.0/10", "10.0.0.0/8", "192.168.0.0/16", "::1/128"]
)
def test_tailscale_and_local_ranges_blocked(dest):
    config = vpn_server.with_private_block(_server_config())
    assert dest in config["routing"]["rules"][2]["ip"]


def test_with_private_block_is_idempotent():
    once = vpn_server.with_private_block(_server_config("reality-alt"))
    assert vpn_server.with_private_block(once) == once


def test_new_inbound_requires_reapply():
    config = vpn_server.with_private_block(_server_config("reality-alt"))
    config["inbounds"].append({"tag": "reality-xhttp", "port": 2087})
    assert not vpn_server.xray_blocks_private(config)


def test_missing_block_outbound_is_added():
    config = _server_config()
    config["outbounds"] = [{"tag": "direct", "protocol": "freedom"}]
    out = vpn_server.with_private_block(config)
    assert {"tag": "block", "protocol": "blackhole"} in out["outbounds"]


def test_fixup_writes_config_and_restarts(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_server_config("reality-alt")))
    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: path)
    written = {}

    def fake_write(config_path, config):
        written["config"] = config
        config_path.write_text(json.dumps(config))

    monkeypatch.setattr(vpn_server, "write_xray_config_with_test", fake_write)
    calls = []
    monkeypatch.setattr(
        vpn_server.subprocess,
        "run",
        lambda args, check=False: calls.append(args) or subprocess.CompletedProcess(args, 0),
    )
    fixup = vpn_server.make_xray_private_block_fixup(_settings())
    assert not fixup.check()
    fixup.apply()
    assert fixup.check()
    assert ["systemctl", "--user", "restart", "xray"] in calls
    assert ["systemctl", "--user", "restart", vpn_server.NODE_UNIT_NAME] in calls


def test_fixup_registered_for_reality_server(monkeypatch):
    FakeRoot(monkeypatch)
    assert "xray-private-block" in [f.id for f in build_fixups(_settings())]
