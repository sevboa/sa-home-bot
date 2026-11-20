from __future__ import annotations

import json
import stat
from unittest.mock import MagicMock

import pytest

from sa_home_bot.config import RealityTransportConfig, RealityXhttpConfig
from sa_home_bot.node.fixups import FixupError, vpn_server
from tests.unit.fixups_world import make_settings


def _base_settings():
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


def _settings_with_xhttp():
    s = _base_settings()
    s.vpn.reality.xhttp = RealityXhttpConfig(
        port=2087,
        sni="www.cloudflare.com",
        dest="www.cloudflare.com:443",
        inbound_tag="reality-xhttp",
        path="/ring",
        mode="auto",
    )
    return s


def _config_with_reality_in():
    return {
        "inbounds": [
            {
                "tag": "reality-in",
                "listen": "0.0.0.0",
                "port": 8443,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": "www.google.com:443",
                        "xver": 0,
                        "serverNames": ["www.google.com"],
                        "privateKey": "SECRET",
                        "shortIds": ["sid"],
                    },
                },
                "sniffing": {"enabled": False},
            },
            {
                "tag": "api-in",
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
        ],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"rules": [{"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"}]},
    }


def test_build_xhttp_inbound_tag_port_network_path_mode_dest_sni_key_preserved():
    config = _config_with_reality_in()
    xhttp = RealityXhttpConfig(
        port=2087,
        sni="www.cloudflare.com",
        dest="www.cloudflare.com:443",
        inbound_tag="reality-xhttp",
        path="/ring",
        mode="auto",
    )
    inbound = vpn_server.build_xhttp_inbound(config, xhttp)
    assert inbound["tag"] == "reality-xhttp"
    assert inbound["port"] == 2087
    ss = inbound["streamSettings"]
    assert ss["network"] == "xhttp"
    assert ss["xhttpSettings"] == {"path": "/ring", "mode": "auto"}
    rs = ss["realitySettings"]
    assert rs["dest"] == "www.cloudflare.com:443"
    assert rs["serverNames"] == ["www.cloudflare.com"]
    assert inbound["settings"]["clients"] == []
    assert rs["privateKey"] == "SECRET"
    assert rs["shortIds"] == ["sid"]
    assert config["inbounds"][0]["tag"] == "reality-in"
    assert config["inbounds"][0]["port"] == 8443


def test_build_xhttp_inbound_no_reality_in_raises():
    config = {"inbounds": [{"tag": "other", "port": 1234}]}
    xhttp = RealityXhttpConfig(sni="www.cloudflare.com", path="/ring")
    with pytest.raises(FixupError, match="в конфиге xray нет входа reality-in"):
        vpn_server.build_xhttp_inbound(config, xhttp)


def _xhttp_inbound_entry():
    return {
        "tag": "reality-xhttp",
        "listen": "0.0.0.0",
        "port": 2087,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": {
            "network": "xhttp",
            "security": "reality",
            "xhttpSettings": {"path": "/ring", "mode": "auto"},
            "realitySettings": {
                "show": False,
                "dest": "www.cloudflare.com:443",
                "xver": 0,
                "serverNames": ["www.cloudflare.com"],
                "privateKey": "SECRET",
                "shortIds": ["sid"],
            },
        },
        "sniffing": {"enabled": False},
    }


def test_xhttp_inbound_matches_true_for_built_inbound():
    config = _config_with_reality_in()
    xhttp = RealityXhttpConfig(
        port=2087,
        sni="www.cloudflare.com",
        dest="www.cloudflare.com:443",
        path="/ring",
        mode="auto",
    )
    config["inbounds"].append(vpn_server.build_xhttp_inbound(config, xhttp))
    assert vpn_server.xhttp_inbound_matches(config, xhttp) is True


def test_xhttp_inbound_matches_false_on_mismatch():
    s = _settings_with_xhttp()
    xhttp = s.vpn.reality.xhttp
    config = _config_with_reality_in()

    entry = _xhttp_inbound_entry()
    config["inbounds"].append(entry)
    assert vpn_server.xhttp_inbound_matches(config, xhttp) is True

    for key, value in (
        ("port", 9999),
        ("path", "/other"),
        ("mode", "packet-up"),
        ("dest", "other.com:443"),
        ("serverNames", ["other.com"]),
    ):
        entry = _xhttp_inbound_entry()
        ss = entry["streamSettings"]
        if key == "port":
            entry[key] = value
        elif key in ("path", "mode"):
            ss["xhttpSettings"][key] = value
        else:
            ss["realitySettings"][key] = value
        config["inbounds"][-1] = entry
        assert vpn_server.xhttp_inbound_matches(config, xhttp) is False, key

    entry = _xhttp_inbound_entry()
    entry["streamSettings"]["network"] = "tcp"
    config["inbounds"][-1] = entry
    assert vpn_server.xhttp_inbound_matches(config, xhttp) is False

    config["inbounds"] = config["inbounds"][:2]
    assert vpn_server.xhttp_inbound_matches(config, xhttp) is False


def test_needed_false_without_xhttp():
    s = _base_settings()
    fixup = vpn_server.make_reality_xhttp_inbound_fixup(s)
    assert fixup.needed(s) is False


def test_needed_true_with_xhttp():
    s = _settings_with_xhttp()
    fixup = vpn_server.make_reality_xhttp_inbound_fixup(s)
    assert fixup.needed(s) is True


def test_apply_clean_config_appends_inbound_calls_adi_preserves_perms_no_temp_files(
    monkeypatch, tmp_path
):
    s = _settings_with_xhttp()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    old_mode = config_path.stat().st_mode

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    lsi_result = MagicMock(
        returncode=0,
        stdout=json.dumps({"inbounds": [{"tag": "reality-in"}, {"tag": "api-in"}]}),
    )
    adi_result = MagicMock(returncode=0, stdout="")
    call_count = {"lsi": 0, "adi": 0}

    def mock_xray_api(args):
        if args[0] == "lsi":
            call_count["lsi"] += 1
            return lsi_result
        if args[0] == "adi":
            call_count["adi"] += 1
            return adi_result
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr(vpn_server, "_xray_api", mock_xray_api)

    def mock_run(argv, **kwargs):
        if argv[1:3] == ["run", "-test"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_xhttp_inbound_fixup(s)
    fixup.apply()

    new_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert len(new_config["inbounds"]) == 3
    assert new_config["inbounds"][0]["tag"] == "reality-in"
    assert new_config["inbounds"][1]["tag"] == "api-in"
    assert new_config["inbounds"][2]["tag"] == "reality-xhttp"
    assert new_config["inbounds"][2]["port"] == 2087
    assert new_config["inbounds"][2]["streamSettings"]["network"] == "xhttp"
    assert new_config["inbounds"][2]["streamSettings"]["xhttpSettings"] == {
        "path": "/ring",
        "mode": "auto",
    }
    assert stat.S_IMODE(config_path.stat().st_mode) == stat.S_IMODE(old_mode)
    assert call_count["adi"] == 1
    assert call_count["lsi"] == 1
    temp_files = list(tmp_path.glob("*.json"))
    assert len(temp_files) == 1
    assert temp_files[0] == config_path
