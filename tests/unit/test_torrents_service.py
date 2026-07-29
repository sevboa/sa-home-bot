"""Служба torrents: describe, добавление по magnet/URL и по base64-файлу,
проверка допустимой директории, пауза/запуск раздач и свободное место."""

import base64

import pytest
import qbittorrentapi

from sa_home_bot.config import Settings, TorrentsConfig
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ERR_INTERNAL, ProtoError
from sa_home_bot.torrents import service as torrents_service
from sa_home_bot.torrents.service import TorrentsService

GIB = 1024**3

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

    def torrents_pause(self, **kwargs):
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        FakeClient.switched.append(("pause", kwargs["torrent_hashes"]))

    def torrents_resume(self, **kwargs):
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        FakeClient.switched.append(("resume", kwargs["torrent_hashes"]))


@pytest.fixture(autouse=True)
def fake_qbittorrent(monkeypatch):
    FakeClient.calls = []
    FakeClient.switched = []
    FakeClient.fail_with = None
    FakeClient.logged_in = False
    FakeClient.logged_out = False
    FakeClient.info = []
    FakeClient.info_kwargs = {}
    monkeypatch.setattr(torrents_service.qbittorrentapi, "Client", FakeClient)
    return FakeClient


@pytest.fixture(autouse=True)
def fake_disk(monkeypatch):
    """Свободное место — не с машины, где идут тесты (на alfred /mnt/data
    существует, в CI нет — иначе результат теста зависел бы от хоста)."""
    usage = {"free": 100 * GIB, "total": 300 * GIB}
    monkeypatch.setattr(
        torrents_service, "_disk_usage", lambda path: (usage["free"], usage["total"])
    )
    return usage


def test_describe_declares_add_action_with_save_path_choices():
    desc = TorrentsService(_settings()).describe()
    assert desc.info.service == "torrents"
    assert desc.capabilities == ("add", "list", "pause", "resume", "space")
    action = desc.find_action("add")
    names = [p.name for p in action.params]
    assert names == ["source", "name", "save_path"]
    save_path_param = next(p for p in action.params if p.name == "save_path")
    assert save_path_param.choices == tuple(SAVE_DIRS)


async def test_add_magnet_calls_torrents_add_with_urls(fake_qbittorrent):
    result = await TorrentsService(_settings()).run_command(
        "add", {"source": "magnet:?xt=urn:btih:abc", "name": "Foo", "save_path": SAVE_DIRS[0]}
    )
    assert result == {"name": "Foo", "save_path": SAVE_DIRS[0], "free_bytes": 100 * GIB}
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
    assert result == {"name": "торрент", "save_path": SAVE_DIRS[1], "free_bytes": 100 * GIB}
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
            "paused": False,
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


async def test_list_marks_paused_in_both_qbittorrent_dialects(fake_qbittorrent):
    """«stalledUP»/«stoppedUP» — внутренние строки qBittorrent (в 5.x paused*
    переименованы в stopped*); признак паузы должен быть явным полем."""
    fake_qbittorrent.info = [
        {"name": "Old", "state": "pausedDL", "progress": 0.5, "dlspeed": 0, "eta": -1},
        {"name": "New", "state": "stoppedUP", "progress": 1.0, "dlspeed": 0, "eta": -1},
        {"name": "Live", "state": "stalledUP", "progress": 1.0, "dlspeed": 0, "eta": -1},
    ]
    result = await TorrentsService(_settings()).run_command("list", {})
    assert [t["paused"] for t in result["torrents"]] == [True, True, False]


async def test_list_api_error_becomes_internal(fake_qbittorrent):
    fake_qbittorrent.fail_with = qbittorrentapi.APIConnectionError("down")
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command("list", {})
    assert excinfo.value.code == ERR_INTERNAL
    assert fake_qbittorrent.logged_out


# --- pause/resume: адресация раздачи по имени (см. докстринг службы) ---

_TORRENTS = [
    {"name": "Foo.S01", "hash": "aaa111", "amount_left": 3 * GIB},
    {"name": "Foo.S02", "hash": "bbb222", "amount_left": 1 * GIB},
    {"name": "Bar.2024", "hash": "ccc333", "amount_left": 0},
]


async def test_pause_by_exact_name_wins_over_substring(fake_qbittorrent):
    """«Foo.S01» — подстрока никого больше, а вот «Foo» было бы неоднозначно:
    точное совпадение проверяется первым (см. _select)."""
    fake_qbittorrent.info = list(_TORRENTS)
    result = await TorrentsService(_settings()).run_command("pause", {"name": "foo.s01"})
    assert result == {"paused": ["Foo.S01"], "count": 1}
    assert fake_qbittorrent.switched == [("pause", ["aaa111"])]


async def test_resume_by_recognisable_part_of_name(fake_qbittorrent):
    fake_qbittorrent.info = list(_TORRENTS)
    result = await TorrentsService(_settings()).run_command("resume", {"name": "bar"})
    assert result == {"resumed": ["Bar.2024"], "count": 1}
    assert fake_qbittorrent.switched == [("resume", ["ccc333"])]


async def test_pause_ambiguous_name_asks_to_be_precise(fake_qbittorrent):
    """Несколько подходящих — не «остановим все похожие на всякий случай»,
    а честная ошибка со списком кандидатов."""
    fake_qbittorrent.info = list(_TORRENTS)
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command("pause", {"name": "Foo"})
    assert excinfo.value.code == ERR_BAD_REQUEST
    assert "Foo.S01" in excinfo.value.message and "Foo.S02" in excinfo.value.message
    assert fake_qbittorrent.switched == []


async def test_pause_all_is_an_explicit_word(fake_qbittorrent):
    fake_qbittorrent.info = list(_TORRENTS)
    result = await TorrentsService(_settings()).run_command("pause", {"name": "все"})
    assert result["count"] == 3
    assert fake_qbittorrent.switched == [("pause", ["aaa111", "bbb222", "ccc333"])]


async def test_pause_unknown_name_lists_what_there_is(fake_qbittorrent):
    fake_qbittorrent.info = list(_TORRENTS)
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command("pause", {"name": "Баз"})
    assert excinfo.value.code == ERR_BAD_REQUEST
    assert "Bar.2024" in excinfo.value.message


async def test_pause_without_name_is_bad_request():
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command("pause", {})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_pause_api_error_becomes_internal(fake_qbittorrent):
    fake_qbittorrent.fail_with = qbittorrentapi.APIConnectionError("down")
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command("pause", {"name": "все"})
    assert excinfo.value.code == ERR_INTERNAL
    assert fake_qbittorrent.logged_out


# --- space: хватает ли места ---


async def test_space_reports_dirs_and_pending_bytes(fake_qbittorrent):
    fake_qbittorrent.info = list(_TORRENTS)
    result = await TorrentsService(_settings()).run_command("space", {})
    assert [d["path"] for d in result["dirs"]] == SAVE_DIRS
    assert result["dirs"][0] == {
        "path": SAVE_DIRS[0],
        "free_bytes": 100 * GIB,
        "total_bytes": 300 * GIB,
    }
    # Уже обещанное текущим закачкам — иначе «свободно 100 ГиБ» вводит в
    # заблуждение, когда 4 из них уже расписаны.
    assert result["downloading_left_bytes"] == 4 * GIB
    assert result["min_free_bytes"] == torrents_service.MIN_FREE_BYTES


async def test_space_survives_dead_qbittorrent(fake_qbittorrent):
    """На вопрос «хватает ли места» сам клиент торрентов не нужен — молчащий
    qBittorrent отнимает только downloading_left_bytes."""
    fake_qbittorrent.fail_with = qbittorrentapi.APIConnectionError("down")
    result = await TorrentsService(_settings()).run_command("space", {})
    assert result["downloading_left_bytes"] is None
    assert result["dirs"][0]["free_bytes"] == 100 * GIB


async def test_space_marks_unmounted_dir(monkeypatch):
    monkeypatch.setattr(torrents_service, "_disk_usage", lambda path: (None, None))
    result = await TorrentsService(_settings()).run_command("space", {})
    assert result["dirs"][0]["free_bytes"] is None
    assert "недоступен" in result["dirs"][0]["error"]


async def test_add_refuses_when_disk_is_almost_full(fake_disk, fake_qbittorrent):
    """Размер раздачи по magnet заранее не знает никто — но добить диск до
    нуля, когда там уже нечего занимать, служба не даёт."""
    fake_disk["free"] = 1 * GIB
    with pytest.raises(ProtoError) as excinfo:
        await TorrentsService(_settings()).run_command(
            "add", {"source": "magnet:?xt=urn:btih:abc", "save_path": SAVE_DIRS[0]}
        )
    assert excinfo.value.code == ERR_BAD_REQUEST
    assert "мало места" in excinfo.value.message
    assert fake_qbittorrent.calls == []  # до qBittorrent дело не дошло
