"""Служба apps: describe из конфига, состояние юнитов, карточка по command,
управление start/stop/restart юнита."""

import pytest

from sa_home_bot.apps import service as apps_service
from sa_home_bot.apps.service import AppsService
from sa_home_bot.config import AppConfig, AppsConfig, Settings
from sa_home_bot.proto.messages import ERR_INTERNAL, ERR_NEEDS_PRIVILEGE, ProtoError


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


def test_describe_declares_app_and_manage_actions():
    desc = AppsService(_settings()).describe()
    assert desc.info.service == "apps"
    assert desc.capabilities == ("qbittorrent", "jellyfin")
    ids = [a.id for a in desc.actions]
    assert ids == ["qbittorrent", "jellyfin", "start", "stop", "restart"]
    # Персональные карточки-действия — без параметров, как раньше.
    assert desc.find_action("qbittorrent").params == ()
    # Управляющие действия — общий параметр name с choices = id приложений.
    for action in ("start", "stop", "restart"):
        spec = desc.find_action(action)
        assert spec.params[0].name == "name"
        assert spec.params[0].choices == ("qbittorrent", "jellyfin")


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


# --- start/stop/restart: реальное управление юнитом ---


async def test_manage_action_unknown_name_raises_bad_request(fake_status):
    with pytest.raises(ProtoError):
        await AppsService(_settings()).run_command("start", {"name": "nope"})


async def test_manage_action_success_returns_fresh_card(fake_status, monkeypatch):
    calls = []

    async def fake_run(action, unit):
        calls.append((action, unit))

    monkeypatch.setattr(apps_service, "_run_systemctl", fake_run)
    result = await AppsService(_settings()).run_command("restart", {"name": "jellyfin"})

    assert calls == [("restart", "jellyfin.service")]
    assert result["id"] == "jellyfin"
    assert result["status"] == "inactive"  # свежая карточка, взята из fake_status


async def test_run_systemctl_permission_denied_raises_needs_privilege(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"sudo: a password is required\n"

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(apps_service.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ProtoError) as excinfo:
        await apps_service._run_systemctl("start", "jellyfin.service")
    assert excinfo.value.code == ERR_NEEDS_PRIVILEGE
    assert "nodectl fix" in excinfo.value.message


async def test_run_systemctl_generic_failure_raises_internal(monkeypatch):
    class FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"Unit jellyfin.service failed to start.\n"

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(apps_service.asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ProtoError) as excinfo:
        await apps_service._run_systemctl("start", "jellyfin.service")
    assert excinfo.value.code == ERR_INTERNAL


async def test_run_systemctl_success_no_raise(monkeypatch):
    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(apps_service.asyncio, "create_subprocess_exec", fake_exec)
    await apps_service._run_systemctl("stop", "jellyfin.service")  # не бросает
