from __future__ import annotations

import io
import urllib.request

from sa_home_bot.utils.http import get_json, get_json_async


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch_urlopen(monkeypatch, body: bytes):
    seen = {}

    def fake_urlopen(target, timeout):
        seen["target"] = target
        seen["timeout"] = timeout
        return FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_get_json_plain_url(monkeypatch):
    seen = patch_urlopen(monkeypatch, b'{"a": 1}')
    assert get_json("http://x/y", 3.0) == {"a": 1}
    assert seen == {"target": "http://x/y", "timeout": 3.0}


def test_get_json_with_headers_builds_request(monkeypatch):
    seen = patch_urlopen(monkeypatch, b"[1]")
    assert get_json("http://x/y", 1.0, {"User-Agent": "t"}) == [1]
    assert isinstance(seen["target"], urllib.request.Request)
    assert seen["target"].get_header("User-agent") == "t"


async def test_get_json_async(monkeypatch):
    patch_urlopen(monkeypatch, b'{"ok": true}')
    assert await get_json_async("http://x", 1.0) == {"ok": True}
