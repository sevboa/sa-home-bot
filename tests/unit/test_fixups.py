"""node/fixups.py — только чистая логика (needed(), генерация sudoers-снипетов,
build_fixups). apply()/check() реальный sudo/файлы не трогаются в тестах."""

import stat

from sa_home_bot.config import AppConfig, AppsConfig, NodeConfig, Settings, TelegramConfig
from sa_home_bot.node import fixups as fixups_module
from sa_home_bot.node.fixups import (
    INSTALL_SMARTMONTOOLS,
    JOURNALCTL_GROUP,
    SMARTCTL_SUDOERS,
    apps_unit_sudoers_content,
    build_fixups,
    make_apps_unit_fixup,
    smartctl_sudoers_content,
    smartctl_wrapper_content,
)


def _make_executable(path):
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _settings(assignments: list[str], apps: list[AppConfig] | None = None) -> Settings:
    return Settings(
        telegram=TelegramConfig(token="x"),
        subscriptions=[],
        node=NodeConfig(assignments=assignments),
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


def test_journalctl_needed_when_monitor_assigned():
    assert JOURNALCTL_GROUP.needed(_settings(["monitor"]))
    assert not JOURNALCTL_GROUP.needed(_settings(["apps"]))


def test_apps_unit_fixup_needed_only_when_apps_assigned():
    app = AppConfig(id="qbittorrent", title="qB", unit="qbittorrent-nox.service")
    fixup = make_apps_unit_fixup(app)
    assert fixup.needed(_settings(["apps"], [app]))
    assert not fixup.needed(_settings(["monitor"], [app]))


# --- Генерация содержимого sudoers-снипетов ---


def test_smartctl_sudoers_content_pins_absolute_path_and_wildcard_args():
    content = smartctl_sudoers_content("/usr/sbin/smartctl", "sevboa")
    assert content == "sevboa ALL=(root) NOPASSWD: /usr/sbin/smartctl *\n"


def test_smartctl_wrapper_content_execs_real_binary_via_sudo():
    content = smartctl_wrapper_content("/usr/sbin/smartctl")
    assert content == '#!/bin/sh\nexec sudo -n /usr/sbin/smartctl "$@"\n'


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


def test_build_fixups_monitor_and_apps_together():
    app = AppConfig(id="jellyfin", title="Jellyfin", unit="jellyfin.service")
    fixups = build_fixups(_settings(["monitor", "apps"], [app]))
    ids = {f.id for f in fixups}
    assert ids == {
        "install-smartmontools",
        "smartctl-sudoers",
        "journalctl-group",
        "apps-unit-sudoers-jellyfin",
    }
