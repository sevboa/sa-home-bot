from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from sa_home_bot.config import AwgAltConfig
from sa_home_bot.node.fixups import FixupError, build_fixups, vpn_server
from tests.unit.fixups_world import make_settings

SECRET = "GENERATED_PRIVATE_KEY_abc="
ALT_IDS = ["awg-alt-interface", "awg-alt-ufw-port", "awg-alt-forward-persist"]


def _settings(alt: AwgAltConfig | None = None):
    s = make_settings(["vpn"])
    s.vpn.awg_alt = alt
    return s


@pytest.fixture
def host(monkeypatch):
    calls: dict[str, list] = {"installed": [], "enabled": [], "run": []}
    state = {"exists": False, "route": "eth0", "genkey_rc": 0}

    def run(argv, **kwargs):
        calls["run"].append(argv)
        return SimpleNamespace(returncode=state["genkey_rc"], stdout=SECRET + "\n", stderr="")

    monkeypatch.setattr(vpn_server.subprocess, "run", run)
    monkeypatch.setattr(vpn_server, "path_exists", lambda p: state["exists"])
    monkeypatch.setattr(vpn_server, "default_route_iface", lambda: state["route"])
    monkeypatch.setattr(vpn_server, "ensure_ip_forward", lambda: None)
    monkeypatch.setattr(vpn_server, "daemon_reload", lambda: None)
    monkeypatch.setattr(vpn_server, "enable_now", lambda unit: calls["enabled"].append(unit))
    monkeypatch.setattr(
        vpn_server,
        "install_root_file",
        lambda path, content, *, mode, make_parents=False: calls["installed"].append(
            (str(path), content, mode)
        ),
    )
    monkeypatch.setattr(vpn_server, "ufw_in_use", lambda: True)
    monkeypatch.setattr(vpn_server, "ufw_state", lambda: "active")
    return SimpleNamespace(calls=calls, state=state)


def test_conf_content_defaults():
    text = vpn_server.awg_alt_conf_content(AwgAltConfig(), "K", 1280, "eth0")
    lines = text.splitlines()
    assert lines[:5] == [
        "[Interface]",
        "Address = 10.9.1.1/24",
        "ListenPort = 51821",
        "PrivateKey = K",
        "MTU = 1280",
    ]
    for key in ("Jc = 5", "S1 = 50", "S2 = 80", "S3 = 0", "S4 = 0", "H1 = 5", "H4 = 8"):
        assert key in lines
    assert not any(line.startswith("I1") for line in lines)
    assert "awg0" not in text
    post_up = next(line for line in lines if line.startswith("PostUp = "))
    assert "-i awg1" in post_up and "-s 10.9.1.0/24 -o eth0 -j MASQUERADE" in post_up


def test_conf_content_ranges_and_i1():
    alt = AwgAltConfig(h1="100-200", s3=12, i1="<b 0xabcd>", i3="<r 10>")
    lines = vpn_server.awg_alt_conf_content(alt, "K", 1280, "eth0").splitlines()
    assert "H1 = 100-200" in lines and "S3 = 12" in lines
    assert "I1 = <b 0xabcd>" in lines and "I3 = <r 10>" in lines
    assert not any(line.startswith("I2") for line in lines)


def test_sudoers_content_extra_interfaces():
    base = vpn_server.awg_sudoers_content("/usr/bin/awg", "awg0", "u")
    assert base == "u ALL=(root) NOPASSWD: /usr/bin/awg show *, /usr/bin/awg set awg0 peer *\n"
    both = vpn_server.awg_sudoers_content("/usr/bin/awg", "awg0", "u", ("awg1",))
    assert both == base[:-1] + ", /usr/bin/awg set awg1 peer *\n"


def test_build_fixups_ids(host):
    without = [f.id for f in build_fixups(_settings())]
    assert not set(ALT_IDS) & set(without)
    with_alt = [f.id for f in build_fixups(_settings(AwgAltConfig()))]
    pos = with_alt.index("awg-forward-persist")
    assert with_alt[pos + 1 : pos + 4] == ALT_IDS
    assert [i for i in with_alt if i not in ALT_IDS] == without


def test_sudoers_check_without_alt_only_checks_presence(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn_server, "SUDOERS_DIR", tmp_path)
    fixup = vpn_server.make_awg_sudoers_fixup(_settings())
    assert fixup.check() is False
    (tmp_path / vpn_server.AWG_SUDOERS_FILE).write_text("anything\n")
    assert fixup.check() is True


def test_sudoers_check_with_alt_compares_content(monkeypatch, tmp_path):
    monkeypatch.setattr(vpn_server, "SUDOERS_DIR", tmp_path)
    monkeypatch.setattr(vpn_server, "find_executable", lambda name: "/usr/bin/awg")
    monkeypatch.setattr(vpn_server, "getuser", lambda: "u")
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        vpn_server,
        "file_matches",
        lambda path, expected: seen.setdefault("expected", expected) == seen.get("current"),
    )
    fixup = vpn_server.make_awg_sudoers_fixup(_settings(AwgAltConfig()))
    seen["current"] = vpn_server.awg_sudoers_content("/usr/bin/awg", "awg0", "u")
    assert fixup.check() is False
    assert "set awg1 peer" in seen["expected"]
    seen["current"] = seen["expected"]
    assert fixup.check() is True
    monkeypatch.setattr(vpn_server, "find_executable", lambda name: None)
    assert fixup.check() is False


def test_interface_apply_writes_new_conf_600(host):
    vpn_server.make_awg_alt_interface_fixup(_settings(AwgAltConfig())).apply()
    [(path, content, mode)] = host.calls["installed"]
    assert path == "/etc/amnezia/amneziawg/awg1.conf"
    assert mode == "0600" and f"PrivateKey = {SECRET}" in content
    assert host.calls["run"] == [["awg", "genkey"]]
    assert host.calls["enabled"] == ["awg-quick@awg1"]


def test_interface_apply_keeps_existing_conf(host):
    host.state["exists"] = True
    vpn_server.make_awg_alt_interface_fixup(_settings(AwgAltConfig())).apply()
    assert host.calls["installed"] == [] and host.calls["run"] == []
    assert host.calls["enabled"] == ["awg-quick@awg1"]


@pytest.mark.parametrize("broken", ["genkey", "route"])
def test_interface_apply_errors_do_not_leak_key(host, caplog, broken):
    caplog.set_level(logging.DEBUG)
    if broken == "genkey":
        host.state["genkey_rc"] = 1
    else:
        host.state["route"] = None
    with pytest.raises(FixupError) as exc:
        vpn_server.make_awg_alt_interface_fixup(_settings(AwgAltConfig())).apply()
    assert SECRET not in str(exc.value) and SECRET not in caplog.text
    assert host.calls["installed"] == []


def test_alt_ufw_and_forward_fixups(host):
    s = _settings(AwgAltConfig(port=51999))
    ufw = vpn_server.make_awg_alt_ufw_fixup(s)
    assert ufw.needed(s) and "51999/udp" in ufw.title
    forward = vpn_server.make_awg_alt_forward_fixup(s)
    assert forward.needed(s) and "awg1" in forward.title
    host.state["exists"] = False
    forward.apply()
    paths = [p for p, _, _ in host.calls["installed"]]
    assert paths == [
        "/usr/local/lib/sa-home-bot/awg1-forward-reapply.sh",
        "/etc/systemd/system/sa-home-awg1-forward.service",
    ]
    script = host.calls["installed"][0][1]
    assert "ip saddr 10.9.1.0/24" in script and "awg0" not in script
