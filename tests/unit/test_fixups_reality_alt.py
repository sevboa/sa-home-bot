import json
import stat
from unittest.mock import MagicMock

import pytest

from sa_home_bot.config import RealityAltConfig, RealityTransportConfig
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


def _settings_with_alt():
    s = _base_settings()
    s.vpn.reality.alt = RealityAltConfig(
        port=2083,
        sni="www.cloudflare.com",
        dest="www.cloudflare.com:443",
        inbound_tag="reality-alt",
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


def test_build_alt_inbound_new_tag_port_dest_sni_empty_clients_key_preserved(
    monkeypatch, tmp_path
):
    config = _config_with_reality_in()
    alt = RealityAltConfig(
        port=2083,
        sni="www.cloudflare.com",
        dest="www.cloudflare.com:443",
        inbound_tag="reality-alt",
    )
    inbound = vpn_server.build_alt_inbound(config, alt)
    assert inbound["tag"] == "reality-alt"
    assert inbound["port"] == 2083
    rs = inbound["streamSettings"]["realitySettings"]
    assert rs["dest"] == "www.cloudflare.com:443"
    assert rs["serverNames"] == ["www.cloudflare.com"]
    assert inbound["settings"]["clients"] == []
    assert rs["privateKey"] == "SECRET"
    assert config["inbounds"][0]["tag"] == "reality-in"
    assert config["inbounds"][0]["port"] == 8443


def test_build_alt_inbound_no_reality_in_raises():
    config = {"inbounds": [{"tag": "other", "port": 1234}]}
    alt = RealityAltConfig(port=2083, sni="www.cloudflare.com", inbound_tag="reality-alt")
    with pytest.raises(FixupError, match="в конфиге xray нет входа reality-in"):
        vpn_server.build_alt_inbound(config, alt)


def test_needed_false_without_alt():
    s = _base_settings()
    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    assert fixup.needed(s) is False


def test_needed_true_with_alt():
    s = _settings_with_alt()
    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    assert fixup.needed(s) is True


def test_apply_clean_config_appends_inbound_calls_adi_preserves_perms_no_temp_files(
    monkeypatch, tmp_path
):
    s = _settings_with_alt()
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

    run_results = []

    def mock_run(argv, **kwargs):
        run_results.append(argv)
        if argv[1:3] == ["run", "-test"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    fixup.apply()

    new_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert len(new_config["inbounds"]) == 3
    assert new_config["inbounds"][0]["tag"] == "reality-in"
    assert new_config["inbounds"][1]["tag"] == "api-in"
    assert new_config["inbounds"][2]["tag"] == "reality-alt"
    assert new_config["inbounds"][2]["port"] == 2083
    assert stat.S_IMODE(config_path.stat().st_mode) == stat.S_IMODE(old_mode)
    assert call_count["adi"] == 1
    assert call_count["lsi"] == 1
    temp_files = list(tmp_path.glob("*.json"))
    assert len(temp_files) == 1
    assert temp_files[0] == config_path


def test_reapply_and_check_after_apply_no_duplicate_no_adi(monkeypatch, tmp_path):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config["inbounds"].append(
        {
            "tag": "reality-alt",
            "listen": "0.0.0.0",
            "port": 2083,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
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
    )
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    lsi_result = MagicMock(
        returncode=0,
        stdout=json.dumps(
            {"inbounds": [{"tag": "reality-in"}, {"tag": "api-in"}, {"tag": "reality-alt"}]}
        ),
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

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    fixup.apply()
    assert fixup.check() is True
    new_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert len(new_config["inbounds"]) == 3
    assert call_count["adi"] == 0


def test_file_exists_tag_missing_in_lsi_calls_adi_file_unchanged(monkeypatch, tmp_path):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config["inbounds"].append(
        {
            "tag": "reality-alt",
            "listen": "0.0.0.0",
            "port": 2083,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
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
    )
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    original_content = config_path.read_text(encoding="utf-8")

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

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    fixup.apply()
    assert call_count["adi"] == 1
    assert config_path.read_text(encoding="utf-8") == original_content


def test_different_settings_raises_fixuperror(monkeypatch, tmp_path):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config["inbounds"].append(
        {
            "tag": "reality-alt",
            "listen": "0.0.0.0",
            "port": 9999,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": "other.com:443",
                    "xver": 0,
                    "serverNames": ["other.com"],
                    "privateKey": "SECRET",
                    "shortIds": ["sid"],
                },
            },
            "sniffing": {"enabled": False},
        }
    )
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    def mock_xray_api(args):
        return MagicMock(returncode=0, stdout=json.dumps({"inbounds": []}))

    monkeypatch.setattr(vpn_server, "_xray_api", mock_xray_api)

    def mock_run(argv, **kwargs):
        if argv[1:3] == ["run", "-test"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    with pytest.raises(FixupError, match="вход уже есть с другими настройками"):
        fixup.apply()


def test_run_test_failure_raises_fixuperror_no_secret_in_error_config_unchanged(
    monkeypatch, tmp_path
):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    original_content = config_path.read_text(encoding="utf-8")

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    def mock_xray_api(args):
        lsi_out = json.dumps({"inbounds": [{"tag": "reality-in"}, {"tag": "api-in"}]})
        return MagicMock(returncode=0, stdout=lsi_out)

    monkeypatch.setattr(vpn_server, "_xray_api", mock_xray_api)

    def mock_run(argv, **kwargs):
        if argv[1:3] == ["run", "-test"]:
            return MagicMock(returncode=1, stdout="", stderr="error")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    with pytest.raises(FixupError, match="xray run -test: код 1") as exc:
        fixup.apply()
    assert "SECRET" not in str(exc.value)
    assert config_path.read_text(encoding="utf-8") == original_content


def test_adi_failure_raises_fixuperror(monkeypatch, tmp_path):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    lsi_out = json.dumps({"inbounds": [{"tag": "reality-in"}, {"tag": "api-in"}]})
    lsi_result = MagicMock(returncode=0, stdout=lsi_out)
    adi_result = MagicMock(returncode=2, stdout="", stderr="fail")

    def mock_xray_api(args):
        if args[0] == "lsi":
            return lsi_result
        if args[0] == "adi":
            return adi_result
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr(vpn_server, "_xray_api", mock_xray_api)

    def mock_run(argv, **kwargs):
        if argv[1:3] == ["run", "-test"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    with pytest.raises(FixupError, match="xray api adi: код 2"):
        fixup.apply()


def test_no_restart_or_systemctl_in_commands(monkeypatch, tmp_path):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    lsi_out = json.dumps({"inbounds": [{"tag": "reality-in"}, {"tag": "api-in"}]})

    mock_xray_api = MagicMock()
    mock_xray_api.side_effect = lambda args: (
        MagicMock(returncode=0, stdout=lsi_out)
        if args[0] == "lsi"
        else MagicMock(returncode=0, stdout="")
    )
    monkeypatch.setattr(vpn_server, "_xray_api", mock_xray_api)

    mock_run = MagicMock()
    mock_run.side_effect = lambda argv, **kwargs: (
        MagicMock(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["run", "-test"]
        else MagicMock(returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    fixup.apply()

    run_calls = [call[0][0] for call in mock_run.call_args_list]
    api_calls = [call[0][0] for call in mock_xray_api.call_args_list]

    for argv in run_calls:
        words = [a for a in argv if "/" not in a]
        assert "restart" not in words
        assert "systemctl" not in words
        assert not argv[0].endswith("systemctl")

    for argv in api_calls:
        words = [a for a in argv if "/" not in a]
        assert "restart" not in words
        assert "systemctl" not in words
        assert not argv[0].endswith("systemctl")


def test_reality_alt_ufw_specs_with_alt():
    s = _settings_with_alt()
    specs = vpn_server.reality_alt_ufw_specs(s)
    assert specs == ["2083/tcp"]


def test_reality_alt_ufw_specs_without_alt():
    s = _base_settings()
    specs = vpn_server.reality_alt_ufw_specs(s)
    assert specs == []


def test_lsi_failure_in_apply_raises_fixuperror(monkeypatch, tmp_path):
    s = _settings_with_alt()
    config_path = tmp_path / "config.json"
    config = _config_with_reality_in()
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    monkeypatch.setattr(vpn_server, "xray_config_path", lambda: config_path)

    def mock_xray_api(args):
        if args[0] == "lsi":
            return MagicMock(returncode=1, stdout="", stderr="fail")
        return MagicMock(returncode=0, stdout="")

    monkeypatch.setattr(vpn_server, "_xray_api", mock_xray_api)

    def mock_run(argv, **kwargs):
        if argv[1:3] == ["run", "-test"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", mock_run)

    fixup = vpn_server.make_reality_alt_inbound_fixup(s)
    with pytest.raises(FixupError, match="xray api lsi не ответил"):
        fixup.apply()
