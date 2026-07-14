"""node/update.py: детектирование установки, парсинг тегов, pipx-вызов.

Реальные pipx/git/сеть не участвуют — subprocess.run/create_subprocess_exec
и importlib.metadata монкипатчатся.
"""

from __future__ import annotations

import importlib.metadata
import json

from sa_home_bot.node import update
from sa_home_bot.utils.version import version_key

# --- version_key ---


def test_version_key_purely_numeric():
    assert version_key("0.21.0") == (0, 21, 0)
    assert version_key("1.2.3") < version_key("1.10.0")  # не лексикографически


def test_version_key_nonnumeric_component_is_zero():
    assert version_key("0.21.0-rc1") == (0, 21, 0)  # "0-rc1" не парсится → 0


# --- installed_version() ---


class _FakeDist:
    def __init__(self, version="1.2.3", direct_url=None):
        self._version = version
        self._direct_url = direct_url

    def read_text(self, name):
        if name == "direct_url.json":
            return self._direct_url
        return None


def test_installed_version_reads_from_metadata(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.9.9")
    assert update.installed_version() == "9.9.9"


def test_installed_version_none_when_not_found(monkeypatch):
    def raise_not_found(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", raise_not_found)
    assert update.installed_version() is None


# --- origin_repo_url() ---


def test_origin_repo_url_editable_install_is_none(monkeypatch):
    direct_url = json.dumps({"dir_info": {"editable": True}, "url": "file:///home/x/repo"})
    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda name: _FakeDist(direct_url=direct_url)
    )
    assert update.origin_repo_url() is None


def test_origin_repo_url_git_install_returns_url(monkeypatch):
    direct_url = json.dumps(
        {
            "url": "https://github.com/sevboa/sa-home-bot.git",
            "vcs_info": {"vcs": "git", "requested_revision": "v0.21.0"},
        }
    )
    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda name: _FakeDist(direct_url=direct_url)
    )
    assert update.origin_repo_url() == "https://github.com/sevboa/sa-home-bot.git"


def test_origin_repo_url_non_git_source_is_none(monkeypatch):
    # Обычный pip install из PyPI — не VCS, обновлять через git+pipx нечего.
    direct_url = json.dumps({"url": "https://pypi.org/simple/sa-home-bot/"})
    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda name: _FakeDist(direct_url=direct_url)
    )
    assert update.origin_repo_url() is None


def test_origin_repo_url_no_direct_url_file(monkeypatch):
    monkeypatch.setattr(
        importlib.metadata, "distribution", lambda name: _FakeDist(direct_url=None)
    )
    assert update.origin_repo_url() is None


def test_origin_repo_url_package_not_found(monkeypatch):
    def raise_not_found(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "distribution", raise_not_found)
    assert update.origin_repo_url() is None


# --- _parse_tags() / latest_tag() ---


def test_parse_tags_extracts_semver_refs():
    output = (
        "abc123\trefs/tags/v0.20.0\n"
        "def456\trefs/tags/v0.21.0\n"
        "ghi789\trefs/tags/not-a-version\n"
        "jkl012\trefs/heads/master\n"
    )
    assert update._parse_tags(output) == ["v0.20.0", "v0.21.0"]


def test_latest_tag_sync_picks_highest_semver_not_lexicographic(monkeypatch):
    # v0.9.0 лексикографически "больше" v0.21.0 — сравнение обязано быть
    # покомпонентным (version_key), а не строковым.
    class FakeCompleted:
        returncode = 0
        stdout = "a\trefs/tags/v0.9.0\nb\trefs/tags/v0.21.0\nc\trefs/tags/v0.2.0\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeCompleted())
    assert update._latest_tag_sync("https://x") == "v0.21.0"


def test_latest_tag_sync_none_on_no_tags(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = "a\trefs/heads/master\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeCompleted())
    assert update._latest_tag_sync("https://x") is None


def test_latest_tag_sync_none_on_nonzero_exit(monkeypatch):
    class FakeCompleted:
        returncode = 128
        stdout = ""
        stderr = "fatal: repository not found"

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeCompleted())
    assert update._latest_tag_sync("https://x") is None


async def test_latest_tag_delegates_to_executor(monkeypatch):
    monkeypatch.setattr(update, "_latest_tag_sync", lambda repo_url: "v0.21.0")
    assert await update.latest_tag("https://x") == "v0.21.0"


async def test_latest_tag_none_on_empty(monkeypatch):
    monkeypatch.setattr(update, "_latest_tag_sync", lambda repo_url: None)
    assert await update.latest_tag("https://x") is None


# --- pipx_reinstall() ---


class _FakeProc:
    def __init__(self, returncode, output=b""):
        self.returncode = returncode
        self._output = output

    async def communicate(self):
        return self._output, None


async def test_pipx_reinstall_success(monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _FakeProc(0, b"installed sa-home-bot 0.22.0\n")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    ok, output = await update.pipx_reinstall("https://x/repo.git", "v0.22.0")

    assert ok is True
    assert "0.22.0" in output
    assert calls[0] == (
        "pipx",
        "install",
        "--force",
        "git+https://x/repo.git@v0.22.0",
    )


async def test_pipx_reinstall_failure_returns_output_tail(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return _FakeProc(1, b"error: something broke\n")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    ok, output = await update.pipx_reinstall("https://x/repo.git", "v0.22.0")

    assert ok is False
    assert "something broke" in output


async def test_pipx_reinstall_handles_os_error(monkeypatch):
    async def fake_exec(*args, **kwargs):
        raise OSError("pipx not found")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    ok, output = await update.pipx_reinstall("https://x/repo.git", "v0.22.0")

    assert ok is False
    assert "pipx not found" in output


async def test_pipx_reinstall_timeout(monkeypatch):
    class HangingProc(_FakeProc):
        async def communicate(self):
            import asyncio

            await asyncio.sleep(10)
            return b"", None

    async def fake_exec(*args, **kwargs):
        return HangingProc(0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(update, "PIPX_INSTALL_TIMEOUT_S", 0.05)
    ok, output = await update.pipx_reinstall("https://x/repo.git", "v0.22.0")
    assert ok is False
