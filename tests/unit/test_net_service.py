"""Служба net: describe, поиск, состояние и коды ошибок."""

from __future__ import annotations

import pytest

from sa_home_bot.config import NetConfig, Settings
from sa_home_bot.net import searxng
from sa_home_bot.net.service import NetService
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ERR_INTERNAL, ProtoError

_RESULTS = [{"title": "T", "url": "https://a.example", "snippet": "S"}]


def _settings(**kwargs) -> Settings:
    return Settings(net=NetConfig(searxng_url="http://127.0.0.1:8888", **kwargs))


def _patch_search(monkeypatch, result=None, raises=None, capture=None):
    async def fake_search(base_url, query, *, limit, timeout, language="ru"):
        if capture is not None:
            capture.append({"query": query, "limit": limit, "timeout": timeout, "lang": language})
        if raises is not None:
            raise raises
        return result if result is not None else _RESULTS

    monkeypatch.setattr(searxng, "search", fake_search)


def test_describe_declares_search_action():
    desc = NetService(_settings()).describe()
    assert desc.info.service == "net"
    assert desc.capabilities == ("search",)
    action = desc.find_action("search")
    assert [p.name for p in action.params] == ["query"]


async def test_search_returns_results(monkeypatch):
    _patch_search(monkeypatch)
    result = await NetService(_settings()).run_command("search", {"query": "погода"})
    assert result == {"query": "погода", "results": _RESULTS, "count": 1}


async def test_search_passes_configured_limit_and_language(monkeypatch):
    calls = []
    _patch_search(monkeypatch, capture=calls)
    await NetService(_settings(max_results=3, language="en")).run_command(
        "search", {"query": "weather"}
    )
    assert calls[0]["limit"] == 3
    assert calls[0]["lang"] == "en"


async def test_search_without_query_is_bad_request(monkeypatch):
    _patch_search(monkeypatch)
    with pytest.raises(ProtoError) as excinfo:
        await NetService(_settings()).run_command("search", {"query": "   "})
    assert excinfo.value.code == ERR_BAD_REQUEST


async def test_search_backend_failure_becomes_internal(monkeypatch):
    _patch_search(monkeypatch, raises=searxng.SearxngError("соединение отклонено"))
    with pytest.raises(ProtoError) as excinfo:
        await NetService(_settings()).run_command("search", {"query": "q"})
    assert excinfo.value.code == ERR_INTERNAL


async def test_get_state_reports_searxng_available(monkeypatch):
    calls = []
    _patch_search(monkeypatch, capture=calls)
    state = await NetService(_settings()).get_state()
    assert state["available"] is True
    assert state["searxng_url"] == "http://127.0.0.1:8888"
    # Проба живости — короткий таймаут, не рабочий: карточка службы не должна
    # висеть полный request_timeout_s из-за мёртвого поисковика.
    assert calls[0]["timeout"] == _settings().net.probe_timeout_s


async def test_get_state_reports_searxng_down(monkeypatch):
    _patch_search(monkeypatch, raises=searxng.SearxngError("не поднят"))
    state = await NetService(_settings()).get_state()
    assert state["available"] is False
    assert "не поднят" in state["error"]


async def test_unknown_action_raises_value_error():
    with pytest.raises(ValueError):
        await NetService(_settings()).run_command("fetch", {})
