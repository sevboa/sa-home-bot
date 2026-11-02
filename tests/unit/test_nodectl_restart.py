from __future__ import annotations

import pytest

from sa_home_bot import nodectl


class _Client:
    result: dict = {}
    calls: list = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def command(self, action, args=None, dst=None):
        _Client.calls.append((action, args))
        return _Client.result


@pytest.fixture
def client(monkeypatch):
    _Client.calls = []
    monkeypatch.setattr(nodectl, "ProtoClient", _Client)
    monkeypatch.setattr(nodectl, "_resolve_endpoint", lambda args: ("unix:///x", None))
    return _Client


@pytest.mark.parametrize(
    ("argv", "expected"),
    [(["restart_node"], None), (["restart_node", "--force"], {"force": True})],
)
def test_restart_node_force_flag(client, capsys, argv, expected):
    client.result = {"scheduled": "restart_node", "delay_s": 1.0}
    assert nodectl.main(argv) == 0
    assert client.calls == [("restart_node", expected)]
    assert "Принято" in capsys.readouterr().out


def test_restart_node_deferred_output(client, capsys):
    client.result = {"deferred": ["devlab"], "message": "рестарт отложен до освобождения: devlab"}
    assert nodectl.main(["restart_node"]) == 0
    out = capsys.readouterr().out
    assert "рестарт отложен до освобождения: devlab" in out
    assert "Принято" not in out
