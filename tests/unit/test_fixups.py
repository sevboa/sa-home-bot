"""node/fixups.py — только чистая логика (needed(), генерация sudoers-снипетов,
build_fixups). apply()/check() реальный sudo/файлы не трогаются в тестах."""

import stat

from sa_home_bot.config import AppConfig, AppsConfig, NodeConfig, Settings, TelegramConfig
from sa_home_bot.node import fixups as fixups_module
from sa_home_bot.node.fixups import (
    INSTALL_SMARTMONTOOLS,
    JOURNALCTL_GROUP,
    NODE_UNIT_SMARTCTL_PATH,
    POWER_CONTROL_POLKIT,
    SMARTCTL_SUDOERS,
    WOL_ENABLE,
    apps_unit_sudoers_content,
    awg_sudoers_content,
    build_fixups,
    make_apps_unit_fixup,
    make_awg_sudoers_fixup,
    make_vpn_probe_forwarding_fixup,
    make_vpn_probe_sudoers_fixup,
    make_vpn_probe_tunnel_fixup,
    microsocks_unit_content,
    mtg_unit_content,
    power_polkit_rule_content,
    proxy_firewall_sudoers_content,
    rewrite_unit_path_line,
    smartctl_sudoers_content,
    smartctl_wrapper_content,
    vpn_probe_sudoers_content,
    vpn_probe_unit_content,
    wol_unit_content,
)


def _make_executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _settings(
    assignments: list[str], apps: list[AppConfig] | None = None, kind: str = "server"
) -> Settings:
    # kind="server" по умолчанию — нейтрально относительно power-control/WoL
    # фиксов (нужны только workstation, см. NodeTraits.power_controllable);
    # так существующие тесты про assignments не задевает несвязанная ось.
    return Settings(
        telegram=TelegramConfig(token="x"),
        subscriptions=[],
        node=NodeConfig(assignments=assignments, kind=kind),
        apps=AppsConfig(items=apps or []),
    )


# --- needed(): какие фиксы актуальны для назначений ноды ---


def test_smartmontools_needed_only_when_monitor_assigned_and_disks_enabled():
    assert INSTALL_SMARTMONTOOLS.needed(_settings(["monitor"]))
    assert not INSTALL_SMARTMONTOOLS.needed(_settings(["apps"]))


def test_smartctl_sudoers_shares_needed_with_install():
    assert SMARTCTL_SUDOERS.needed(_settings(["monitor"])) == INSTALL_SMARTMONTOOLS.needed(
        _settings(["monitor"])
    )


def test_node_unit_smartctl_path_shares_needed_with_install():
    assert NODE_UNIT_SMARTCTL_PATH.needed(_settings(["monitor"])) == INSTALL_SMARTMONTOOLS.needed(
        _settings(["monitor"])
    )


def test_journalctl_needed_when_monitor_assigned():
    assert JOURNALCTL_GROUP.needed(_settings(["monitor"]))
    assert not JOURNALCTL_GROUP.needed(_settings(["apps"]))


def test_apps_unit_fixup_needed_only_when_apps_assigned():
    app = AppConfig(id="qbittorrent", title="qB", unit="qbittorrent-nox.service")
    fixup = make_apps_unit_fixup(app)
    assert fixup.needed(_settings(["apps"], [app]))
    assert not fixup.needed(_settings(["monitor"], [app]))


def test_awg_sudoers_needed_only_when_vpn_assigned():
    fixup = make_awg_sudoers_fixup(_settings(["vpn"]))
    assert fixup.needed(_settings(["vpn"]))
    assert not fixup.needed(_settings(["apps"]))


# --- Генерация содержимого sudoers-снипетов ---


def test_smartctl_sudoers_content_pins_absolute_path_and_wildcard_args():
    content = smartctl_sudoers_content("/usr/sbin/smartctl", "sevboa")
    assert content == "sevboa ALL=(root) NOPASSWD: /usr/sbin/smartctl *\n"


def test_smartctl_wrapper_content_execs_real_binary_via_sudo():
    content = smartctl_wrapper_content("/usr/sbin/smartctl")
    assert content == '#!/bin/sh\nexec sudo -n /usr/sbin/smartctl "$@"\n'


# --- rewrite_unit_path_line(): чинит уже развёрнутые юниты (живой баг на
# mycraft — PATH резолвился до pipx-venv вместо ~/.local/bin, см. setup_wizard).


def test_rewrite_unit_path_line_moves_wrapper_dir_first():
    content = (
        "[Service]\n"
        "Environment=PATH=/home/sevboa/.local/share/pipx/venvs/sa-home-bot/bin"
        ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n"
        "TimeoutStopSec=150\n"
    )
    new_content = rewrite_unit_path_line(content, "/home/sevboa/.local/bin")
    assert (
        "Environment=PATH=/home/sevboa/.local/bin"
        ":/home/sevboa/.local/share/pipx/venvs/sa-home-bot/bin"
        ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n" in new_content
    )
    # Остальные строки юнита не тронуты.
    assert "[Service]\n" in new_content
    assert "TimeoutStopSec=150\n" in new_content


def test_rewrite_unit_path_line_returns_none_without_path_line():
    content = "[Service]\nExecStart=/bin/true\n"
    assert rewrite_unit_path_line(content, "/home/sevboa/.local/bin") is None


def test_rewrite_unit_path_line_dedupes_wrapper_dir_already_present():
    content = "Environment=PATH=/usr/local/bin:/home/sevboa/.local/bin:/usr/bin\n"
    new_content = rewrite_unit_path_line(content, "/home/sevboa/.local/bin")
    assert new_content == "Environment=PATH=/home/sevboa/.local/bin:/usr/local/bin:/usr/bin\n"


# --- _which(): фолбэк на sbin-каталоги, которых обычно нет в PATH обычного
# пользователя по SSH (см. deploy/sa-home-node.service — там PATH их содержит,
# а интерактивный логин-шелл — обычно нет). Баг живьём поймали на alfred:
# smartctl/visudo стоят в /usr/sbin, а nodectl fix их не находил.


def test_which_falls_back_to_sbin_dirs_when_not_in_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    _make_executable(tmp_path / "visudo")
    assert fixups_module._which("visudo") == str(tmp_path / "visudo")


def test_which_returns_none_when_nowhere_found(tmp_path, monkeypatch):
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    assert fixups_module._which("does-not-exist") is None


def test_smartmontools_check_falls_back_to_sbin_dirs(tmp_path, monkeypatch):
    # Живой баг 2026-08-01 на mycraft: apt честно ставит smartctl в
    # /usr/sbin, а check() голым shutil.which (без фолбэка на sbin, в
    # отличие от _real_smartctl_path) считал его отсутствующим — фикс вечно
    # висел "команда прошла, но проверка отрицательна".
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    _make_executable(tmp_path / "smartctl")
    assert INSTALL_SMARTMONTOOLS.check()


def test_install_sudoers_snippet_raises_fixup_error_without_visudo(monkeypatch):
    monkeypatch.setattr(fixups_module, "_which", lambda name: None)
    try:
        fixups_module._install_sudoers_snippet("x", "content")
    except fixups_module.FixupError as exc:
        assert "visudo" in str(exc)
    else:
        raise AssertionError("ожидался FixupError")


def test_real_smartctl_path_falls_back_to_sbin(tmp_path, monkeypatch):
    monkeypatch.setattr(fixups_module.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(fixups_module, "_SBIN_FALLBACK_DIRS", (str(tmp_path),))
    _make_executable(tmp_path / "smartctl")
    assert fixups_module._real_smartctl_path() == str(tmp_path / "smartctl")


def test_awg_sudoers_content_pins_absolute_path_and_interface():
    content = awg_sudoers_content("/usr/local/bin/awg", "awg0", "sevboa")
    assert "/usr/local/bin/awg show *" in content
    assert "/usr/local/bin/awg set awg0 peer *" in content
    # Не разрешаем изменение приватного ключа/порта интерфейса — только пиров.
    assert "private-key" not in content
    assert "awg-quick" not in content


def test_mtg_unit_content_embeds_port_and_secret():
    content = mtg_unit_content(443, "sekret")
    assert "simple-run 0.0.0.0:443 sekret" in content
    assert "/usr/local/bin/mtg" in content


def test_microsocks_unit_content_binds_given_host_and_port():
    content = microsocks_unit_content("100.111.4.42", 1080, "/usr/bin/microsocks")
    assert "/usr/bin/microsocks -i 100.111.4.42 -p 1080" in content


def test_proxy_firewall_sudoers_content_only_two_named_counters():
    content = proxy_firewall_sudoers_content("/usr/sbin/nft", "sevboa")
    assert "/usr/sbin/nft -j list counter inet filter mtg_bytes" in content
    assert "/usr/sbin/nft -j list counter inet filter socks_bytes" in content
    # Не весь nft — иначе это было бы равносильно root на firewall.
    assert content.count("NOPASSWD:") == 1


def test_build_fixups_includes_proxy_fixups_when_vpn_assigned():
    ids = {f.id for f in build_fixups(_settings(["vpn"]))}
    assert {"proxy-units", "proxy-firewall"} <= ids


def test_apps_unit_sudoers_content_only_start_stop_restart_of_this_unit():
    app = AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service")
    content = apps_unit_sudoers_content(app, "/usr/bin/systemctl", "sevboa")
    assert content == (
        "sevboa ALL=(root) NOPASSWD: "
        "/usr/bin/systemctl start jellyfin.service, "
        "/usr/bin/systemctl stop jellyfin.service, "
        "/usr/bin/systemctl restart jellyfin.service\n"
    )
    # Ни другого юнита, ни произвольных systemctl-команд снипет не разрешает.
    assert "qbittorrent" not in content
    assert " reload " not in content


# --- build_fixups(): фильтрация по needed() ---


def test_build_fixups_empty_for_bare_node():
    assert build_fixups(_settings([])) == []


def test_build_fixups_includes_apps_unit_fixup_per_app():
    apps = [
        AppConfig(id="qbittorrent", title="qB", unit="qbittorrent-nox.service"),
        AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service"),
    ]
    fixups = build_fixups(_settings(["apps"], apps))
    ids = {f.id for f in fixups}
    assert "apps-unit-sudoers-qbittorrent" in ids
    assert "apps-unit-sudoers-jellyfin" in ids
    assert "install-smartmontools" not in ids  # monitor не назначен


def test_build_fixups_includes_awg_sudoers_when_vpn_assigned():
    ids = {f.id for f in build_fixups(_settings(["vpn"]))}
    assert "awg-sudoers" in ids


def test_build_fixups_monitor_and_apps_together():
    app = AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service")
    fixups = build_fixups(_settings(["monitor", "apps"], [app]))
    ids = {f.id for f in fixups}
    assert ids == {
        "install-smartmontools",
        "smartctl-sudoers",
        "node-unit-smartctl-path",
        "journalctl-group",
        "apps-unit-sudoers-jellyfin",
    }


# --- power-control-polkit / wol-enable: только workstation (always_on=False) —
# server/vps недоступность которых уже авария, добровольно уводить в офлайн
# (даже своей же кнопкой в боте) не предлагается вообще, см. node/kind.py.


def test_power_control_and_wol_needed_only_for_workstation():
    for kind in ("server", "vps"):
        settings = _settings([], kind=kind)
        assert not POWER_CONTROL_POLKIT.needed(settings)
        assert not WOL_ENABLE.needed(settings)
    settings = _settings([], kind="workstation")
    assert POWER_CONTROL_POLKIT.needed(settings)
    assert WOL_ENABLE.needed(settings)


def test_build_fixups_includes_power_and_wol_only_for_workstation():
    ids = {f.id for f in build_fixups(_settings([], kind="workstation"))}
    assert {"power-control-polkit", "wol-enable"} <= ids
    for kind in ("server", "vps"):
        ids = {f.id for f in build_fixups(_settings([], kind=kind))}
        assert "power-control-polkit" not in ids
        assert "wol-enable" not in ids


def test_power_polkit_rule_content_lists_login1_actions_for_user():
    content = power_polkit_rule_content("sevboa")
    assert 'subject.user == "sevboa"' in content
    for action in ("power-off", "reboot", "suspend"):
        assert f'"org.freedesktop.login1.{action}"' in content
    assert "polkit.Result.YES" in content


def test_wol_unit_content_runs_ethtool_wol_g_on_given_iface():
    content = wol_unit_content("/usr/sbin/ethtool", "enp1s0")
    assert "ExecStart=/usr/sbin/ethtool -s enp1s0 wol g\n" in content
    assert "Type=oneshot" in content


def test_vpn_probe_needed_only_when_vpn_check_assigned():
    fixup = make_vpn_probe_tunnel_fixup(_settings(["vpn_check"]))
    assert fixup.needed(_settings(["vpn_check"]))
    assert not fixup.needed(_settings(["vpn"]))


def test_vpn_probe_sudoers_shares_needed_with_tunnel():
    settings = _settings(["vpn_check"])
    assert make_vpn_probe_sudoers_fixup(settings).needed(
        settings
    ) == make_vpn_probe_tunnel_fixup(settings).needed(settings)


def test_build_fixups_includes_vpn_probe_fixups_when_vpn_check_assigned():
    ids = {f.id for f in build_fixups(_settings(["vpn_check"]))}
    assert {"vpn-check-probe-tunnel", "vpn-check-probe-sudoers"} <= ids


def test_build_fixups_excludes_vpn_probe_fixups_without_vpn_check():
    ids = {f.id for f in build_fixups(_settings(["vpn"]))}
    assert "vpn-check-probe-tunnel" not in ids
    assert "vpn-check-probe-sudoers" not in ids


def test_vpn_probe_tunnel_check_false_when_unit_content_is_stale(monkeypatch):
    """Живой застрявший апгрейд 2026-08-17: юнит существует и active, но с
    ДРУГИМ (старым, netns-версии) содержимым — check() обязан это заметить,
    иначе apply() с новым содержимым не позовётся вовсе."""
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: True)
    monkeypatch.setattr(fixups_module, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(fixups_module, "_read_privileged", lambda path: "stale old content\n")
    called_is_active = []
    monkeypatch.setattr(
        fixups_module.subprocess,
        "run",
        lambda *a, **k: called_is_active.append(True) or type("R", (), {"returncode": 0})(),
    )
    settings = _settings(["vpn_check"])
    assert fixups_module._vpn_probe_tunnel_check(settings) is False
    assert not called_is_active  # содержимое не совпало — до is-active дело не дошло


def test_vpn_probe_tunnel_check_true_when_content_matches_and_active(monkeypatch):
    monkeypatch.setattr(fixups_module, "_privileged_exists", lambda path: True)
    monkeypatch.setattr(fixups_module, "_which", lambda name: f"/usr/bin/{name}")

    def fake_read(path):
        return fixups_module.vpn_probe_unit_content(
            "vpn-probe", fixups_module.VPN_PROBE_IFACE, "/usr/bin/ip", "/usr/bin/awg-quick"
        )

    monkeypatch.setattr(fixups_module, "_read_privileged", fake_read)
    monkeypatch.setattr(
        fixups_module.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})()
    )
    settings = _settings(["vpn_check"])
    assert fixups_module._vpn_probe_tunnel_check(settings) is True


def test_vpn_probe_unit_content_runs_awg_quick_inside_netns():
    content = vpn_probe_unit_content(
        "vpn-probe", "awg-probe0", "/usr/sbin/ip", "/usr/bin/awg-quick"
    )
    assert (
        "ExecStart=/usr/sbin/ip netns exec vpn-probe /usr/bin/awg-quick up awg-probe0\n" in content
    )
    assert (
        "ExecStop=/usr/sbin/ip netns exec vpn-probe /usr/bin/awg-quick down awg-probe0\n"
        in content
    )
    assert "RemainAfterExit=yes" in content


def test_vpn_probe_unit_content_pre_steps_setup_netns_and_veth_idempotently():
    content = vpn_probe_unit_content(
        "vpn-probe", "awg-probe0", "/usr/sbin/ip", "/usr/bin/awg-quick"
    )
    # Ведущий "-" — не валить юнит, если шаг уже применён (повторный `add`
    # уже существующего netns/veth просто вернёт ошибку, которую игнорируем).
    assert "ExecStartPre=-/usr/sbin/ip netns add vpn-probe\n" in content
    assert (
        "ExecStartPre=-/usr/sbin/ip link add vprobe-veth0 type veth peer name vprobe-veth1\n"
        in content
    )
    assert "ExecStartPre=-/usr/sbin/ip link set vprobe-veth1 netns vpn-probe\n" in content
    # "set ... up" идемпотентно само по себе — без ведущего "-".
    assert "ExecStartPre=/usr/sbin/ip link set vprobe-veth0 up\n" in content
    assert (
        "ExecStartPre=-/usr/sbin/ip netns exec vpn-probe /usr/sbin/ip route add default "
        "via 10.200.200.1\n" in content
    )


def test_vpn_probe_sudoers_content_pins_ip_path_and_netns_curl_wildcard():
    content = vpn_probe_sudoers_content("/usr/sbin/ip", "vpn-probe", "sevboa")
    assert content == "sevboa ALL=(root) NOPASSWD: /usr/sbin/ip netns exec vpn-probe curl *\n"


def test_vpn_probe_forwarding_needed_same_as_tunnel():
    settings = _settings(["vpn_check"])
    forwarding = make_vpn_probe_forwarding_fixup(settings)
    tunnel = make_vpn_probe_tunnel_fixup(settings)
    assert forwarding.needed(settings) == tunnel.needed(settings)


def test_find_base_chain_returns_none_without_matching_hook(monkeypatch):
    monkeypatch.setattr(
        fixups_module,
        "_nft_json_ruleset",
        lambda: [
            {
                "chain": {
                    "hook": "input", "type": "filter", "family": "inet",
                    "table": "filter", "name": "input",
                }
            }
        ],
    )
    assert fixups_module._find_base_chain("forward", "filter") is None


def test_find_base_chain_finds_existing_forward_chain(monkeypatch):
    ruleset = [
        {
            "chain": {
                "hook": "input", "type": "filter", "family": "inet",
                "table": "filter", "name": "input",
            }
        },
        {
            "chain": {
                "hook": "forward", "type": "filter", "family": "inet",
                "table": "filter", "name": "forward",
            }
        },
    ]
    monkeypatch.setattr(fixups_module, "_nft_json_ruleset", lambda: ruleset)
    assert fixups_module._find_base_chain("forward", "filter") == ("inet", "filter", "forward")


def test_probe_forwarding_check_looks_for_veth_name_and_subnet(monkeypatch):
    monkeypatch.setattr(
        fixups_module,
        "_nft_ruleset_text",
        lambda: 'iifname "vprobe-veth0" accept\nip saddr 10.200.200.0/30 masquerade\n',
    )
    assert fixups_module._probe_forwarding_check() is True


def test_probe_forwarding_check_false_when_absent(monkeypatch):
    monkeypatch.setattr(fixups_module, "_nft_ruleset_text", lambda: "")
    assert fixups_module._probe_forwarding_check() is False


def test_prepare_probe_conf_strips_dns_line():
    raw = (
        "[Interface]\n"
        "PrivateKey = SECRET\n"
        "Address = 10.9.0.15/32\n"
        "DNS = 1.1.1.1\n"
        "\n"
        "[Peer]\n"
        "PublicKey = PUB\n"
    )
    prepared = fixups_module._prepare_probe_conf(raw)
    assert "DNS" not in prepared  # иначе awg-quick зовёт отсутствующий resolvconf
    assert "PrivateKey = SECRET" in prepared
    assert "[Peer]" in prepared


def test_run_fixups_survives_check_raising_and_still_applies_it():
    """Живой краш 2026-08-17: check() кинул FixupError (sudo без TTY/кэша)
    и уронил весь run_fixups, оставив ВСЕ фиксы после него непроверенными —
    не только тот, что упал."""
    applied = []
    calls = {"n": 0}

    def flaky_check() -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise fixups_module.FixupError("sudo без TTY")
        return True  # после apply() проверка снова доступна и подтверждает успех

    flaky = fixups_module.Fixup(
        id="flaky",
        title="Проверка иногда падает",
        needed=lambda settings: True,
        check=flaky_check,
        apply=lambda: applied.append("flaky"),
    )
    after = fixups_module.Fixup(
        id="after",
        title="Идёт следующим",
        needed=lambda settings: True,
        check=lambda: True,
        apply=lambda: applied.append("after"),
    )
    failed = fixups_module.run_fixups([flaky, after])
    assert failed == []
    assert applied == ["flaky"]  # apply() всё же вызван, несмотря на упавшую первую проверку


def test_run_fixups_survives_check_raising_after_apply_too():
    """check() падает и до, и после apply() — фикс уходит в failed, но
    следующий за ним фикс в списке всё равно проверяется (не крашится весь run)."""
    after_checked = []

    def always_raises() -> bool:
        raise fixups_module.FixupError("недоступно")

    flaky = fixups_module.Fixup(
        id="flaky",
        title="Всегда падает",
        needed=lambda settings: True,
        check=always_raises,
        apply=lambda: None,
    )
    after = fixups_module.Fixup(
        id="after",
        title="Идёт следующим",
        needed=lambda settings: True,
        check=lambda: after_checked.append(True) or True,
        apply=lambda: None,
    )
    failed = fixups_module.run_fixups([flaky, after])
    assert failed == ["flaky"]
    assert after_checked  # run не упал целиком — до "after" дошли
