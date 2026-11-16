import urllib.request

import pytest

from sa_home_bot.node import power_rule


@pytest.mark.parametrize(
    ("status", "webui_model", "expected"),
    [
        ({}, "", []),
        ({"active": 2, "serving": ["a", "b"]}, "", ["модели: a, b"]),
        ({"active": 3}, "", ["модели: 3 запросов"]),
        ({"waiting": [{}, {}]}, "", ["модели: ждут запуска"]),
        ({"companion": {"active": 1}}, "", ["Альфред отвечает"]),
        ({"companion": None}, "", []),
        ({"requested_ago_s": {"m": 100}}, "m", ["webui: писали 100 с назад"]),
        ({"requested_ago_s": {"m": 2000}}, "m", []),
        ({"requested_ago_s": {"m": 100}}, "", []),
        ({"requested_ago_s": {"n": 100}}, "m", []),
        (
            {"active": 1, "serving": ["x"], "requested_ago_s": {"m": 5}},
            "m",
            ["модели: x", "webui: писали 5 с назад"],
        ),
    ],
)
def test_switch_blockers(status, webui_model, expected):
    assert power_rule.switch_blockers(status, webui_model, 1800.0) == expected


async def test_fetch_switch_status_parses_json(monkeypatch):
    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return self.body

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda url, timeout=None: FakeResponse(b'{"active": 1}'),
    )
    assert await power_rule.fetch_switch_status("http://x") == {"active": 1}


async def test_fetch_switch_status_error_returns_none(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("refused")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert await power_rule.fetch_switch_status("http://x") is None
