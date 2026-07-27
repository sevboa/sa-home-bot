"""Служба torrents: describe, добавление по magnet/URL и по base64-файлу,
проверка допустимой директории."""

import base64

import pytest
import qbittorrentapi

from sa_home_bot.config import Settings, TorrentsConfig
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ERR_INTERNAL, ProtoError
from sa_home_bot.torrents import service as torrents_service
from sa_home_bot.torrents.service import TorrentsService

SAVE_DIRS = [
    "/mnt/data/torrents/complete",
    "/mnt/data/pr",
    "/mnt/scratch/torrents/complete",
    "/mnt/scratch/pr",
]


def _settings() -> Settings:
    return Settings(
        torrents=TorrentsConfig(
            qbittorrent_url="http://example:8080",
            qbittorrent_user="sevboa",
            qbittorrent_password="secret",
            save_dirs=list(SAVE_DIRS),
        )
    )


class FakeClient:
    calls: list[tuple[tuple, dict]] = []
    fail_with: Exception | None = None
    logged_in = False
    logged_out = False

    def __init__(self, **kwargs):
        FakeClient.init_kwargs = kwargs

    def auth_log_in(self):
        FakeClient.logged_in = True

    def auth_log_out(self):
        FakeClient.logged_out = True

    def torrents_add(self, *args, **kwargs):
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        FakeClient.calls.append((args, kwargs))

    def torrents_info(self, **kwargs):
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        FakeClient.info_kwargs = kwargs
        return FakeClient.info


@pytest.fixture(autouse=True)
def fake_qbittorrent(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail_with = None
    FakeClient.logged_in = False
    FakeClient.logged_out = False
    FakeClient.info = []
    FakeClient.info_kwargs = {}
    monkeypatch.setattr(torrents_service.qbittorrentapi, "Client", FakeClient)
    return FakeClient


def test_describe_declares_add_action_with_save_path_choices():
    desc = TorrentsService(_settings()).describe()
    assert desc.info.service == "torrents"
    assert desc.capabilities == ("add", "list")
    action = desc.find_action("add")
    names = [p.name for p in action.params]
    assert names == ["source", "name", "save_path"]
    save_path_param = next(p for p in action.params if p.name == "save_path")
    assert save_path_param.choices == tuple(SAVE_DIRS)


async def test_add_magnet_calls_torrents_add_with_urls(fake_qbittorrent):
    result = await TorrentsService(_settings()).run_command(
        "add", {"source": "magnet:?xt=urn:btih:abc", "name": "Foo", "save_path": SAVE_DIRS[0]}
    )
    assert result == {"name": "Foo", "save_path": SAVE_DIRS[0]}
    args, kwargs = fake_qbittorrent.calls[0]
    assert kwargs["urls"] == "magnet:?xt=urn:btih:abc"
    assert kwargs["save_path"] == SAVE_DIRS[0]
    assert "torrent_files" not in kwargs
    assert fake_qbittorrent.logged_in and fake_qbittorrent.logged_out


async def test_add_base64_file_calls_torrents_add_with_bytes(fake_qbittorrent):
    raw = b"d8:announce...e"
    source = base64.b64encode(raw).decode()
    result = await TorrentsService(_settings()).run_command(
        "add", {"source": source, "save_path": SAVE_DIRS[1]}
    )
    assert result == {"name": "торрент", "save_path": SAVE_DIRS[1]}
    _, kwargs = fake_qbittorrent.calls[0]
    assert kwargs["torrent_files"] == raw
    assert "urls" not in kwargs


async def test_add_invalid_base64_raises_bad_request():
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command(
            "add", {"source": "not-base64-!!", "save_path": SAVE_DIRS[0]}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_add_unknown_save_path_raises_bad_request():
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command(
            "add", {"source": "magnet:?xt=urn:btih:abc", "save_path": "/etc"}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_add_missing_source_raises_bad_request():
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command(
            "add", {"source": "", "save_path": SAVE_DIRS[0]}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_qbittorrent_api_error_becomes_internal(fake_qbittorrent):
    fake_qbittorrent.fail_with = qbittorrentapi.APIConnectionError("down")
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command(
            "add", {"source": "magnet:?xt=urn:btih:abc", "save_path": SAVE_DIRS[0]}
        )
    assert excinfo.value.code == ERR_INTERNAL
    assert fake_qbittorrent.logged_out  # finally логаутится даже при ошибке


async def test_command_unknown_action_raises_value_error():
    with pytest.raises(ValueError):
        await TorrentsService(_settings()).run_command("remove", {})


# --- list: read-only состояние закачек (для /ai-тула swarm_status) ---


async def test_list_returns_narrow_slice_without_paths(fake_qbittorrent):
    fake_qbittorrent.info = [
        {
            "name": "Foo.S01",
            "state": "downloading",
            "progress": 0.4237,
            "dlspeed": 1048576,
            "eta": 3600,
            "save_path": "/mnt/scratch/torrents/incomplete",
            "hash": "abc",
        }
    ]
    result = await TorrentsService(_settings()).run_command("list", {})
    assert result["count"] == 1
    assert result["torrents"] == [
        {
            "name": "Foo.S01",
            "state": "downloading",
            "progress_pct": 42,
            "dlspeed_bytes_s": 1048576,
            "eta_s": 3600,
        }
    ]
    # Путь на диске и хэш наружу не уезжают — см. докстринг службы.
    assert "save_path" not in result["torrents"][0]
    assert "hash" not in result["torrents"][0]


async def test_list_limits_and_sorts_on_qbittorrent_side(fake_qbittorrent):
    await TorrentsService(_settings()).run_command("list", {})
    assert fake_qbittorrent.info_kwargs == {
        "sort": "dlspeed",
        "reverse": True,
        "limit": torrents_service.LIST_LIMIT,
    }


async def test_list_hides_placeholder_eta(fake_qbittorrent):
    """qBittorrent отдаёт 8640000 как «неизвестно» — модель не должна
    пересказывать это как реальные 100 суток."""
    fake_qbittorrent.info = [
        {"name": "Seeding", "state": "stalledUP", "progress": 1.0, "dlspeed": 0, "eta": 8640000},
        {"name": "Paused", "state": "pausedDL", "progress": 0.1, "dlspeed": 0, "eta": -1},
    ]
    result = await TorrentsService(_settings()).run_command("list", {})
    assert [t["eta_s"] for t in result["torrents"]] == [None, None]
    assert result["torrents"][0]["progress_pct"] == 100


async def test_list_api_error_becomes_internal(fake_qbittorrent):
    fake_qbittorrent.fail_with = qbittorrentapi.APIConnectionError("down")
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command("list", {})
    assert excinfo.value.code == ERR_INTERNAL
    assert fake_qbittorrent.logged_out
