"""APK-поток службы vpn: свежесть по (фейковому) GitHub API, кэш на диске,
проверка размера/sha256, чанкование, сброс file_id, поведение при
недоступном GitHub (stale)."""

from __future__ import annotations

import base64
import hashlib

import pytest
import pytest_asyncio

from sa_home_bot.config import Settings, VpnConfig
from sa_home_bot.db.connection import Database
from sa_home_bot.db.migrations import apply_migrations
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import apk as apk_client
from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn.service import VpnService


class DummyAwg:
    async def server_public_key(self):
        return "server-pub"

    async def add_peer(self, public_key, address):
        pass

    async def remove_peer(self, public_key):
        pass

    async def transfer(self):
        return {}

    async def latest_handshakes(self):
        return {}

    async def generate_keypair(self):
        return "priv", "pub"


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(tmp_path / "vpn.sqlite")
    await db.open()
    await apply_migrations(db)
    events: list[tuple[str, dict]] = []

    async def emit(event_type, data):
        events.append((event_type, data))

    cfg = VpnConfig(apk_cache_dir=tmp_path / "apk-cache")
    svc = VpnService(Settings(vpn=cfg), db, DummyAwg(), emit)
    yield svc, tmp_path
    await db.close()


def _release(tag: str, content: bytes, *, with_digest: bool = True) -> dict:
    asset = {
        "name": "amneziawg-2.0.1.apk",
        "size": len(content),
        "browser_download_url": "https://example.invalid/amneziawg.apk",
    }
    if with_digest:
        asset["digest"] = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return {"tag_name": tag, "assets": [asset]}


async def test_apk_info_downloads_and_caches(env, monkeypatch):
    svc, tmp_path = env
    content = b"apk-bytes-v1"

    monkeypatch.setattr(
        apk_client, "latest_release_sync", lambda repo, timeout: _release("2.0.1", content)
    )

    def fake_download(url, dest, timeout):
        dest.write_bytes(content)

    monkeypatch.setattr(apk_client, "download_sync", fake_download)

    info = await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    assert info["version"] == "2.0.1"
    assert info["size"] == len(content)
    assert info["sha256"] == hashlib.sha256(content).hexdigest()
    assert info["stale"] is False
    cached = list((tmp_path / "apk-cache").glob("*.apk"))
    assert len(cached) == 1
    assert cached[0].read_bytes() == content


async def test_apk_info_skips_redownload_when_version_unchanged(env, monkeypatch):
    svc, _tmp_path = env
    content = b"same-bytes"
    calls = {"n": 0}

    monkeypatch.setattr(
        apk_client, "latest_release_sync", lambda repo, timeout: _release("2.0.1", content)
    )

    def fake_download(url, dest, timeout):
        calls["n"] += 1
        dest.write_bytes(content)

    monkeypatch.setattr(apk_client, "download_sync", fake_download)

    await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    svc._apk_checked_at = None  # обойти memo, чтобы дошло до сверки версии
    await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    assert calls["n"] == 1


async def test_apk_info_sha_mismatch_does_not_replace_cache(env, monkeypatch):
    svc, tmp_path = env
    real_content = b"real-bytes"
    release = _release("2.0.1", real_content)
    # digest в релизе не совпадёт с тем, что реально скачается.

    def fake_download(url, dest, timeout):
        dest.write_bytes(b"corrupted-bytes")

    monkeypatch.setattr(apk_client, "latest_release_sync", lambda repo, timeout: release)
    monkeypatch.setattr(apk_client, "download_sync", fake_download)

    with pytest.raises(ProtoError):
        await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    assert not list((tmp_path / "apk-cache").glob("*.apk"))
    # временный файл тоже не должен оставаться мусором.
    assert not list((tmp_path / "apk-cache").glob(".*"))


async def test_apk_info_stale_when_github_unavailable_but_cache_exists(env, monkeypatch):
    svc, _tmp_path = env
    content = b"cached-bytes"
    monkeypatch.setattr(
        apk_client, "latest_release_sync", lambda repo, timeout: _release("2.0.1", content)
    )
    monkeypatch.setattr(
        apk_client, "download_sync", lambda url, dest, timeout: dest.write_bytes(content)
    )
    await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})

    svc._apk_checked_at = None

    def fail(repo, timeout):
        raise apk_client.ApkFetchError("недоступен")

    monkeypatch.setattr(apk_client, "latest_release_sync", fail)
    info = await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    assert info["stale"] is True
    assert info["version"] == "2.0.1"  # старый кэш всё равно отдаётся


async def test_apk_chunk_roundtrip_and_eof(env, monkeypatch):
    svc, _tmp_path = env
    content = bytes(range(256)) * 10  # 2560 байт
    monkeypatch.setattr(
        apk_client, "latest_release_sync", lambda repo, timeout: _release("2.0.1", content)
    )
    monkeypatch.setattr(
        apk_client, "download_sync", lambda url, dest, timeout: dest.write_bytes(content)
    )
    await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})

    collected = b""
    offset = 0
    for _ in range(10):
        chunk = await svc.run_command(
            vpn_protocol.ACTION_APK_CHUNK, {"offset": offset, "length": 1000}
        )
        data = base64.b64decode(chunk["data_b64"])
        collected += data
        offset += len(data)
        if chunk["eof"]:
            break
    assert collected == content


async def test_apk_set_file_id_persists_and_reset_on_new_version(env, monkeypatch):
    svc, _tmp_path = env
    content_v1 = b"v1"
    monkeypatch.setattr(
        apk_client, "latest_release_sync", lambda repo, timeout: _release("1.0.0", content_v1)
    )
    monkeypatch.setattr(
        apk_client, "download_sync", lambda url, dest, timeout: dest.write_bytes(content_v1)
    )
    await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    await svc.run_command(
        vpn_protocol.ACTION_APK_SET_FILE_ID, {"telegram_file_id": "tg-file-1"}
    )
    svc._apk_checked_at = None
    info = await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    assert info["telegram_file_id"] == "tg-file-1"

    content_v2 = b"v2-bytes-different"
    monkeypatch.setattr(
        apk_client, "latest_release_sync", lambda repo, timeout: _release("2.0.0", content_v2)
    )
    monkeypatch.setattr(
        apk_client, "download_sync", lambda url, dest, timeout: dest.write_bytes(content_v2)
    )
    svc._apk_checked_at = None
    info = await svc.run_command(vpn_protocol.ACTION_APK_INFO, {})
    assert info["version"] == "2.0.0"
    assert info["telegram_file_id"] is None
