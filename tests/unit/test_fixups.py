import json
import stat
from pathlib import Path

import pytest

from sa_home_bot.config import AppConfig
from sa_home_bot.node.fixups import (
    FixupError,
    build_fixups,
    core,
    host,
    netfilter,
    privileged,
    proxy,
    vpn_server,
)
from tests.unit.fixups_world import FakeRoot, make_settings

UFW_WOOSTER = (
    "Status: active\n\nTo        Action      From\n--        ------      ----\n"
    "41641/udp                  ALLOW       Anywhere                   # tailscale\n"
    "443/udp                    ALLOW       Anywhere                   # awg\n"
)
UFW_CLEAN = "Status: active\n\n41641/udp   ALLOW   Anywhere\n"
UFW_STATUS = "/usr/bin/ufw status"
APP = AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service")


@pytest.fixture
def root(monkeypatch):
    return FakeRoot(monkeypatch)


def _make_executable(path: Path) -> None:
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.mark.parametrize(
    ("raises_after_apply", "expected_failed"), [(False, []), (True, ["flaky"])]
)
def test_run_fixups_survives_check_raising(raises_after_apply, expected_failed):
    applied, after_checked = [], []
    check_calls = []

    def flaky_check() -> bool:
        check_calls.append(1)
        if len(check_calls) == 1 or raises_after_apply:
            raise FixupError("sudo без TTY")
        return True

    flaky = core.Fixup(
        id="flaky",
        title="Проверка иногда падает",
        needed=lambda settings: True,
        check=flaky_check,
        apply=lambda: applied.append("flaky"),
    )
    after = core.Fixup(
        id="after",
        title="Идёт следующим",
        needed=lambda settings: True,
        check=lambda: after_checked.append(True) or True,
        apply=lambda: None,
    )

    assert core.run_fixups([flaky, after]) == expected_failed
    assert applied == ["flaky"]
    assert after_checked


def test_run_fixups_reports_failed_apply(capsys):
    broken = core.Fixup(
        id="broken",
        title="Ломается",
        needed=lambda settings: True,
        check=lambda: False,
        apply=lambda: (_ for _ in ()).throw(FixupError("нет прав")),
    )
    assert core.run_fixups([broken]) == ["broken"]
    assert "❌ Ломается: нет прав" in capsys.readouterr().out


_JEEVES_APPS = [
    AppConfig(id="qbittorrent", title="qB", unit="qbittorrent-nox.service"),
    APP,
]


@pytest.mark.parametrize(
    ("assignments", "kind", "apps", "expected"),
    [
        ([], "server", [], set()),
        ([], "vps", [], set()),
        ([], "workstation", [], {"power-control-polkit", "wol-enable"}),
        (
            ["monitor"],
            "server",
            [],
            {
                "install-smartmontools",
                "smartctl-sudoers",
                "node-unit-smartctl-path",
                "journalctl-group",
            },
        ),
        (
            ["apps"],
            "server",
            _JEEVES_APPS,
            {"apps-unit-sudoers-qbittorrent", "apps-unit-sudoers-jellyfin"},
        ),
        (
            ["monitor", "apps"],
            "server",
            [APP],
            {
                "install-smartmontools",
                "smartctl-sudoers",
                "node-unit-smartctl-path",
                "journalctl-group",
                "apps-unit-sudoers-jellyfin",
            },
        ),
        (["vpn"], "server", [], {"awg-sudoers", "proxy-units", "proxy-firewall"}),
    ],
)
def test_build_fixups_follows_assignments_and_kind(root, assignments, kind, apps, expected):
    root.missing_tools = {"ufw"}
    root.outputs["nft list tables"] = "table inet filter\n"
    ids = {f.id for f in build_fixups(make_settings(assignments, apps, kind))}
    assert ids == expected


def test_awg_fixups_skipped_when_vpn_carries_only_reality(root):
    root.outputs[UFW_STATUS] = UFW_WOOSTER
    settings = make_settings(["vpn"])
    settings.vpn.transports = ["reality"]
    ids = {f.id for f in build_fixups(settings)}
    assert not ids & {"awg-sudoers", "awg-ufw-port", "awg-forward-persist", "proxy-units"}


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            host.smartctl_sudoers_content("/usr/sbin/smartctl", "sevboa"),
            "sevboa ALL=(root) NOPASSWD: /usr/sbin/smartctl *\n",
        ),
        (
            host.smartctl_wrapper_content("/usr/sbin/smartctl"),
            '#!/bin/sh\nexec sudo -n /usr/sbin/smartctl "$@"\n',
        ),
        (
            host.apps_unit_sudoers_content(APP, "/usr/bin/systemctl", "sevboa"),
            "sevboa ALL=(root) NOPASSWD: "
            "/usr/bin/systemctl start jellyfin.service, "
            "/usr/bin/systemctl stop jellyfin.service, "
            "/usr/bin/systemctl restart jellyfin.service\n",
        ),
        (
            vpn_server.awg_sudoers_content("/usr/local/bin/awg", "awg0", "sevboa"),
            "sevboa ALL=(root) NOPASSWD: /usr/local/bin/awg show *, "
            "/usr/local/bin/awg set awg0 peer *\n",
        ),
        (
            proxy.proxy_firewall_sudoers_content("/usr/sbin/nft", "sevboa"),
            "sevboa ALL=(root) NOPASSWD: "
            "/usr/sbin/nft -j list counter inet filter mtg_bytes, "
            "/usr/sbin/nft -j list counter inet filter socks_bytes\n",
        ),
    ],
)
def test_sudoers_and_wrapper_content_is_exact(content, expected):
    assert content == expected


def test_power_polkit_rule_lists_login1_actions_for_user():
    content = host.power_polkit_rule_content("sevboa")
    assert 'subject.user == "sevboa"' in content
    for action in ("power-off", "reboot", "suspend"):
        assert f'"org.freedesktop.login1.{action}"' in content
    assert "polkit.Result.YES" in content


def test_wol_unit_runs_ethtool_wol_g_on_given_iface():
    content = host.wol_unit_content("/usr/sbin/ethtool", "enp1s0")
    assert "ExecStart=/usr/sbin/ethtool -s enp1s0 wol g\n" in content
    assert "Type=oneshot" in content


def test_proxy_unit_contents_embed_endpoint():
    assert "simple-run 0.0.0.0:443 sekret" in proxy.mtg_unit_content(443, "sekret")
    assert "/usr/local/bin/mtg" in proxy.mtg_unit_content(443, "sekret")
    unit = proxy.microsocks_unit_content("100.111.4.42", 1080, "/usr/bin/microsocks")
    assert "/usr/bin/microsocks -i 100.111.4.42 -p 1080" in unit


_UNIT_PATH_LINE = "Environment=PATH=/usr/local/bin:/home/sevboa/.local/bin:/usr/bin\n"


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "[Service]\nEnvironment=PATH=/home/sevboa/.local/share/pipx/venvs/sa-home-bot/bin"
            ":/usr/local/bin:/usr/bin\nTimeoutStopSec=150\n",
            "[Service]\nEnvironment=PATH=/home/sevboa/.local/bin"
            ":/home/sevboa/.local/share/pipx/venvs/sa-home-bot/bin:/usr/local/bin:/usr/bin\n"
            "TimeoutStopSec=150\n",
        ),
        (
            _UNIT_PATH_LINE,
            "Environment=PATH=/home/sevboa/.local/bin:/usr/local/bin:/usr/bin\n",
        ),
        ("[Service]\nExecStart=/bin/true\n", None),
    ],
)
def test_rewrite_unit_path_line_puts_wrapper_dir_first(content, expected):
    assert host.rewrite_unit_path_line(content, "/home/sevboa/.local/bin") == expected


@pytest.fixture
def sbin_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(privileged.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(privileged, "SBIN_FALLBACK_DIRS", (str(tmp_path),))
    return tmp_path


def test_find_executable_falls_back_to_sbin_dirs(sbin_dir):
    _make_executable(sbin_dir / "visudo")
    assert privileged.find_executable("visudo") == str(sbin_dir / "visudo")
    assert privileged.find_executable("does-not-exist") is None


def test_real_smartctl_path_falls_back_to_sbin(sbin_dir):
    _make_executable(sbin_dir / "smartctl")
    assert host.real_smartctl_path() == str(sbin_dir / "smartctl")


def test_real_smartctl_path_skips_wrapper_dir(monkeypatch):
    seen = []
    wrapper_dir = str(host.SMARTCTL_WRAPPER_PATH.parent)
    monkeypatch.setenv("PATH", f"{wrapper_dir}:/usr/bin")
    monkeypatch.setattr(privileged.shutil, "which", lambda name, path=None: seen.append(path))
    monkeypatch.setattr(privileged, "SBIN_FALLBACK_DIRS", ())
    assert host.real_smartctl_path() is None
    assert seen == ["/usr/bin"]


def test_smartmontools_check_sees_binary_in_sbin(sbin_dir):
    _make_executable(sbin_dir / "smartctl")
    assert host.INSTALL_SMARTMONTOOLS.check()


def test_install_sudoers_snippet_validates_with_visudo_then_installs(root):
    privileged.install_sudoers_snippet("50-x", "sevboa ALL=(root) NOPASSWD: /bin/true\n")
    assert root.calls[0][:2] == ["/usr/bin/visudo", "-cf"]
    assert root.installed["/etc/sudoers.d/50-x"] == "sevboa ALL=(root) NOPASSWD: /bin/true\n"
    assert root.sudo_commands("install")[0][1:3] == ["-m", "0440"]


def test_install_sudoers_snippet_needs_visudo(root):
    root.missing_tools = {"visudo"}
    with pytest.raises(FixupError, match="visudo"):
        privileged.install_sudoers_snippet("x", "content")


def test_power_control_apply_installs_polkit_package_when_rules_dir_missing(root, monkeypatch):
    monkeypatch.setattr(
        privileged, "install_argv", lambda package: ["apt-get", "install", "-y", package]
    )
    host.POWER_CONTROL_POLKIT.apply()
    assert root.sudo_calls[0] == ["apt-get", "install", "-y", "polkitd"]
    assert root.sudo_commands("install")[0] == [
        "install",
        "-d",
        "-m",
        "0755",
        "/etc/polkit-1/rules.d",
    ]
    assert "polkit.addRule" in root.installed[str(host.POWER_POLKIT_RULE_FILE)]


def test_power_control_apply_skips_package_when_rules_dir_exists(root, monkeypatch):
    root.dirs = {"/etc/polkit-1/rules.d"}
    monkeypatch.setattr(privileged, "install_argv", lambda package: ["apt-get", package])
    host.POWER_CONTROL_POLKIT.apply()
    assert ["apt-get", "polkitd"] not in root.sudo_calls


def _ufw_fixup_state(root, ufw_status):
    if ufw_status is None:
        root.missing_tools = {"ufw"}
    else:
        root.outputs[UFW_STATUS] = ufw_status


@pytest.mark.parametrize(
    ("ufw_status", "needed"), [(UFW_CLEAN, True), ("Status: inactive\n", False), (None, False)]
)
def test_awg_ufw_and_forward_needed_only_with_active_or_unknown_ufw(root, ufw_status, needed):
    _ufw_fixup_state(root, ufw_status)
    settings = make_settings(["vpn"])
    assert vpn_server.make_awg_ufw_fixup(settings).needed(settings) is (
        needed and ufw_status is not None
    )
    forward_needed = vpn_server.make_awg_forward_fixup(settings).needed(settings)
    assert forward_needed is (ufw_status == UFW_CLEAN)
    ids = {f.id for f in build_fixups(settings)}
    assert ("awg-forward-persist" in ids) is forward_needed


def test_awg_ufw_check_true_when_port_already_allowed(root):
    root.outputs[UFW_STATUS] = UFW_WOOSTER
    settings = make_settings(["vpn"])
    settings.vpn.endpoint_port = 443
    assert vpn_server.make_awg_ufw_fixup(settings).check()


def test_awg_ufw_apply_opens_udp_port_on_clean_node(root):
    root.outputs[UFW_STATUS] = UFW_CLEAN
    settings = make_settings(["vpn"])
    settings.vpn.endpoint_port = 51820
    fixup = vpn_server.make_awg_ufw_fixup(settings)
    assert not fixup.check()
    fixup.apply()
    assert root.sudo_commands("/usr/bin/ufw")[-1][:3] == ["/usr/bin/ufw", "allow", "51820/udp"]


def test_sub_ufw_opens_only_missing_ports(root):
    root.outputs[UFW_STATUS] = UFW_CLEAN + "8444/tcp ALLOW Anywhere\n"
    settings = make_settings(["vpn"])
    settings.vpn.reality = object()
    fixup = vpn_server.make_sub_ufw_fixup(settings)
    assert fixup.needed(settings) and not fixup.check()
    fixup.apply()
    allowed = [c[2] for c in root.sudo_commands("/usr/bin/ufw") if c[1] == "allow"]
    assert allowed == ["80/tcp"]


def _forward_world(root, *, existing: bool):
    root.outputs[UFW_STATUS] = UFW_CLEAN
    root.outputs["/usr/bin/ip -4 route show default"] = "default via 192.168.0.1 dev eth0\n"
    root.files["/proc/sys/net/ipv4/ip_forward"] = "1\n"
    if existing:
        for path in vpn_server.awg_forward_paths("awg0"):
            root.files[str(path)] = "ручной файл"


def test_awg_forward_check_needs_existing_active_unit(root):
    _forward_world(root, existing=True)
    fixup = vpn_server.make_awg_forward_fixup(make_settings(["vpn"]))
    assert not fixup.check()
    root.active_units = {"sa-home-awg0-forward.service"}
    assert fixup.check()


def test_awg_forward_apply_installs_script_and_unit_on_clean_node(root):
    _forward_world(root, existing=False)
    vpn_server.make_awg_forward_fixup(make_settings(["vpn"])).apply()
    assert list(root.installed) == [
        "/usr/local/lib/sa-home-bot/awg0-forward-reapply.sh",
        "/etc/systemd/system/sa-home-awg0-forward.service",
    ]
    assert ["systemctl", "enable", "--now", "sa-home-awg0-forward.service"] in root.sudo_calls


def test_awg_forward_apply_keeps_hand_installed_files(root):
    _forward_world(root, existing=True)
    vpn_server.make_awg_forward_fixup(make_settings(["vpn"])).apply()
    assert root.installed == {}


def test_awg_forward_script_has_forward_nat_and_mss_clamp_rules():
    text = vpn_server.awg_forward_script_content("awg0", "10.9.0.0/24", "eth0", 1280)
    assert "nft add rule ip filter FORWARD iifname awg0 accept" in text
    assert "ip saddr 10.9.0.0/24 oifname eth0 masquerade" in text
    assert "maxseg size set 1240" in text


def _nft_chain(hook, name, chain_type="filter"):
    return {
        "chain": {
            "hook": hook,
            "type": chain_type,
            "family": "inet",
            "table": "filter",
            "name": name,
        }
    }


@pytest.mark.parametrize(
    ("ruleset", "expected"),
    [
        ([_nft_chain("input", "input")], None),
        (
            [_nft_chain("input", "input"), _nft_chain("forward", "forward")],
            ("inet", "filter", "forward"),
        ),
    ],
)
def test_find_base_chain(root, ruleset, expected):
    root.outputs["nft -j list ruleset"] = json.dumps({"nftables": ruleset})
    assert netfilter.find_base_chain("forward", "filter") == expected


def test_ensure_ip_forward_noop_when_already_enabled(root):
    root.files["/proc/sys/net/ipv4/ip_forward"] = "1\n"
    netfilter.ensure_ip_forward()
    assert root.sudo_calls == []


def test_ensure_ip_forward_persists_sysctl_and_applies_live(root):
    root.files["/proc/sys/net/ipv4/ip_forward"] = "0\n"
    netfilter.ensure_ip_forward()
    assert root.installed[str(netfilter.IP_FORWARD_SYSCTL_PATH)] == "net.ipv4.ip_forward = 1\n"
    assert ["sysctl", "-w", "net.ipv4.ip_forward=1"] in root.sudo_calls


def _nft_json(*, counters_only: bool) -> str:
    counter_objects = [
        {"counter": {"family": "inet", "table": "filter", "name": n, "packets": 0, "bytes": 0}}
        for n in ("mtg_bytes", "socks_bytes")
    ]
    if counters_only:
        return json.dumps({"nftables": counter_objects})
    rules = [
        {
            "rule": {
                "family": "inet",
                "table": "filter",
                "chain": "input",
                "expr": [{"counter": n}, {"accept": None}],
            }
        }
        for n in ("mtg_bytes", "socks_bytes")
    ]
    return json.dumps({"nftables": counter_objects + rules})


@pytest.mark.parametrize(("counters_only", "applied"), [(True, False), (False, True)])
def test_proxy_firewall_check_requires_rules_not_just_counters(root, counters_only, applied):
    root.outputs["nft -j list table inet filter"] = _nft_json(counters_only=counters_only)
    assert proxy._proxy_firewall_check() is applied


@pytest.mark.parametrize(
    ("tables", "needed"),
    [("table ip filter\ntable ip nat\n", False), ("table inet filter\n", True), (None, True)],
)
def test_proxy_firewall_needed_unless_inet_filter_known_missing(root, tables, needed):
    root.outputs["nft list tables"] = tables
    assert (
        proxy.make_proxy_firewall_fixup(make_settings(["vpn"])).needed(make_settings(["vpn"]))
        is needed
    )


_WORKING_NFTABLES_CONF = (
    "#!/usr/sbin/nft -f\n"
    "flush ruleset\n"
    "table inet filter {\n"
    "  chain input {\n"
    "    type filter hook input priority 0; policy drop;\n"
    "    ct state established,related accept\n"
    '    iifname "tailscale0" accept\n'
    "    tcp dport 443 accept\n"
    "  }\n"
    "  chain forward {\n"
    "    type filter hook forward priority 0; policy drop;\n"
    '    iifname "awg0" accept\n'
    "  }\n"
    "}\n"
    "table ip nat {\n"
    "  chain postrouting {\n"
    "    type nat hook postrouting priority 100;\n"
    '    ip saddr 10.9.0.0/24 oifname "eth0" masquerade\n'
    "  }\n"
    "}\n"
)


def test_append_nftables_conf_counters_keeps_file_loadable(root):
    root.outputs["cat /etc/nftables.conf"] = _WORKING_NFTABLES_CONF
    proxy._append_nftables_conf_counters(make_settings(["vpn"]))

    checked = next(c for c in root.sudo_calls if c[:3] == ["/usr/bin/nft", "-c", "-f"])
    install = root.sudo_commands("install")[0]
    assert checked[-1] == install[-2]
    out = root.installed["/etc/nftables.conf"]
    body = out.splitlines()
    table_i = body.index("table inet filter {")
    chain_i = body.index("  chain input {")
    hook_i = next(i for i, s in enumerate(body) if s.lstrip().startswith("type filter hook input"))
    obj_i = next(i for i, s in enumerate(body) if "counter mtg_bytes { }" in s)
    rule_i = next(i for i, s in enumerate(body) if "counter name mtg_bytes accept" in s)
    assert table_i < obj_i < chain_i
    assert "counter socks_bytes { }" in out
    assert hook_i < rule_i
    assert 'iifname "tailscale0" tcp dport 1080 counter name socks_bytes accept' in out


def test_append_nftables_conf_counters_idempotent(root):
    root.outputs["cat /etc/nftables.conf"] = _WORKING_NFTABLES_CONF.replace(
        "  chain input {\n", "  counter mtg_bytes { }\n  chain input {\n"
    )
    proxy._append_nftables_conf_counters(make_settings(["vpn"]))
    assert root.sudo_calls == [["cat", "/etc/nftables.conf"]]


def test_nftables_conf_with_counters_refuses_unexpected_structure():
    with pytest.raises(FixupError, match="структура файла не та"):
        proxy.nftables_conf_with_counters("table ip nat {\n}\n", make_settings(["vpn"]))
