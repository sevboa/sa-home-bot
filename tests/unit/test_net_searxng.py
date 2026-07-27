"""Клиент локального SearXNG: сборка URL, срез результатов, обработка сбоев.
Реальный HTTP не идёт — подменяется _get_json_sync (как в test_llm_ollama)."""

from __future__ import annotations

import urllib.error

import pytest

from sa_home_bot.net import searxng

BASE = "http://127.0.0.1:8888"

_PAYLOAD = {
    "results": [
        {"title": "Первый", "url": "https://a.example", "content": "Описание раз"},
        {"title": "Второй", "url": "https://b.example", "content": "Описание два"},
        {"title": "Третий", "url": "https://c.example", "content": "Описание три"},
    ]
}


def _fake(payload, capture=None):
    def _get_json_sync(url, timeout):
        if capture is not None:
            capture.append((url, timeout))
        if isinstance(payload, Exception):
            raise payload
        return payload

    return _get_json_sync


async def test_search_returns_narrow_slice(monkeypatch):
    monkeypatch.setattr(searxng, "_get_json_sync", _fake(_PAYLOAD))
    results = await searxng.search(BASE, "погода", limit=2, timeout=5)
    assert results == [
        {"title": "Первый", "url": "https://a.example", "snippet": "Описание раз"},
        {"title": "Второй", "url": "https://b.example", "snippet": "Описание два"},
    ]


async def test_search_requests_json_format_explicitly(monkeypatch):
    """format=json в settings.yml выключен по умолчанию — если его не
    попросить явно, вернётся HTML (главная ловушка §9 плана)."""
    calls = []
    monkeypatch.setattr(searxng, "_get_json_sync", _fake(_PAYLOAD, calls))
    await searxng.search(BASE, "что-то", limit=1, timeout=5)
    url, timeout = calls[0]
    assert "format=json" in url
    assert url.startswith(f"{BASE}/search?")
    assert timeout == 5


async def test_search_url_encodes_query(monkeypatch):
    calls = []
    monkeypatch.setattr(searxng, "_get_json_sync", _fake(_PAYLOAD, calls))
    await searxng.search(BASE, "курс доллара", limit=1, timeout=5)
    assert "q=%D0%BA%D1%83%D1%80%D1%81+%D0%B4%D0%BE%D0%BB%D0%BB%D0%B0%D1%80%D0%B0" in calls[0][0]


async def test_search_trims_long_snippets(monkeypatch):
    long_text = "слово " * 200
    payload = {"results": [{"title": "T", "url": "u", "content": long_text}]}
    monkeypatch.setattr(searxng, "_get_json_sync", _fake(payload))
    results = await searxng.search(BASE, "q", limit=1, timeout=5)
    assert len(results[0]["snippet"]) <= searxng.SNIPPET_LIMIT + 1  # +1 на «…»
    assert results[0]["snippet"].endswith("…")


async def test_search_empty_results_is_not_an_error(monkeypatch):
    monkeypatch.setattr(searxng, "_get_json_sync", _fake({"results": []}))
    assert await searxng.search(BASE, "чепуха", limit=5, timeout=5) == []


async def test_search_network_error_becomes_searxng_error(monkeypatch):
    monkeypatch.setattr(searxng, "_get_json_sync", _fake(urllib.error.URLError("отказано")))
    with pytest.raises(searxng.SearxngError):
        await searxng.search(BASE, "q", limit=5, timeout=5)


async def test_search_html_instead_of_json_becomes_searxng_error(monkeypatch):
    """format=json не включили в settings.yml — json.loads падает на ValueError."""
    monkeypatch.setattr(searxng, "_get_json_sync", _fake(ValueError("не JSON")))
    with pytest.raises(searxng.SearxngError):
        await searxng.search(BASE, "q", limit=5, timeout=5)


async def test_search_rejects_non_object_payload(monkeypatch):
    monkeypatch.setattr(searxng, "_get_json_sync", _fake([1, 2, 3]))
    with pytest.raises(searxng.SearxngError):
        await searxng.search(BASE, "q", limit=5, timeout=5)
