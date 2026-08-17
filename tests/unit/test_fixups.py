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
    make_vpn_probe_sudoers_fixup,
    make_vpn_probe_tunnel_fixup,
    power_polkit_rule_content,
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


def test_vpn_probe_sudoers_content_pins_ip_path_and_netns_curl_wildcard():
    content = vpn_probe_sudoers_content("/usr/sbin/ip", "vpn-probe", "sevboa")
    assert content == "sevboa ALL=(root) NOPASSWD: /usr/sbin/ip netns exec vpn-probe curl *\n"
