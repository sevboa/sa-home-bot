"""Служба apps: describe из конфига, состояние юнитов, карточка по command."""

import pytest

from sa_home_bot.apps import service as apps_service
from sa_home_bot.apps.service import AppsService
from sa_home_bot.config import AppConfig, AppsConfig, Settings


def _settings() -> Settings:
    return Settings(
        apps=AppsConfig(
            items=[
                AppConfig(
                    id="qbittorrent",
                    title="🧲 qBittorrent",
                    unit="qbittorrent-nox.service",
                    urls=["http://example:8080"],
                ),
                AppConfig(id="jellyfin", title="🎬 Jellyfin", unit="jellyfin.service"),
            ]
        )
    )


@pytest.fixture
def fake_status(monkeypatch):
    statuses = {"qbittorrent-nox.service": "active", "jellyfin.service": "inactive"}

    async def _read(unit: str) -> str:
        return statuses.get(unit, "unknown")

    monkeypatch.setattr(apps_service, "read_unit_status", _read)
    return statuses


def test_describe_declares_app_actions():
    desc = AppsService(_settings()).describe()
    assert desc.info.service == "apps"
    assert desc.capabilities == ("qbittorrent", "jellyfin")
    assert [(a.id, a.title) for a in desc.actions] == [
        ("qbittorrent", "🧲 qBittorrent"),
        ("jellyfin", "🎬 Jellyfin"),
    ]
    assert all(not a.params for a in desc.actions)


async def test_get_state_reports_all_apps(fake_status):
    state = await AppsService(_settings()).get_state()
    by_id = {a["id"]: a for a in state["apps"]}
    assert by_id["qbittorrent"]["status"] == "active"
    assert by_id["qbittorrent"]["urls"] == ["http://example:8080"]
    assert by_id["jellyfin"]["status"] == "inactive"


async def test_command_returns_app_card(fake_status):
    result = await AppsService(_settings()).run_command("qbittorrent", {})
    assert result["title"] == "🧲 qBittorrent"
    assert result["unit"] == "qbittorrent-nox.service"
    assert result["status"] == "active"


async def test_command_unknown_app_raises(fake_status):
    with pytest.raises(ValueError):
        await AppsService(_settings()).run_command("nope", {})
