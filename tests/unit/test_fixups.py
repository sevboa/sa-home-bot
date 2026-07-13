"""node/fixups.py — только чистая логика (needed(), генерация sudoers-снипетов,
build_fixups). apply()/check() реальный sudo/файлы не трогаются в тестах."""

from sa_home_bot.config import AppConfig, AppsConfig, NodeConfig, Settings, TelegramConfig
from sa_home_bot.node.fixups import (
    INSTALL_SMARTMONTOOLS,
    JOURNALCTL_GROUP,
    SMARTCTL_SUDOERS,
    apps_unit_sudoers_content,
    build_fixups,
    make_apps_unit_fixup,
    smartctl_sudoers_content,
)


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
