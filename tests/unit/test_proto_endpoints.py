"""Разбор endpoint'ов транспорта: unix-путь, unix:-префикс, tcp://host:port."""

from pathlib import Path

import pytest

from sa_home_bot.proto.endpoints import (
    TcpEndpoint,
    UnixEndpoint,
    parse_endpoint,
    resolve_endpoint,
)


def test_plain_path_is_unix():
    ep = parse_endpoint("./data/node.sock")
    assert ep == UnixEndpoint(Path("./data/node.sock"))
    assert str(ep) == "data/node.sock"


def test_path_object_is_unix():
    assert parse_endpoint(Path("/tmp/x.sock")) == UnixEndpoint(Path("/tmp/x.sock"))


def test_unix_prefix():
    assert parse_endpoint("unix:./data/x.sock") == UnixEndpoint(Path("data/x.sock"))


def test_unix_url_form():
    assert parse_endpoint("unix:///tmp/x.sock") == UnixEndpoint(Path("/tmp/x.sock"))


def test_tcp():
    ep = parse_endpoint("tcp://127.0.0.1:8710")
    assert ep == TcpEndpoint("127.0.0.1", 8710)
    assert str(ep) == "tcp://127.0.0.1:8710"


def test_tcp_hostname_and_brackets():
    assert parse_endpoint("tcp://alfred.tailnet-example.ts.net:8710").host.endswith("ts.net")
    assert parse_endpoint("tcp://[::1]:8710") == TcpEndpoint("::1", 8710)


def test_endpoint_passthrough():
    ep = TcpEndpoint("h", 1)
    assert parse_endpoint(ep) is ep


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "tcp://127.0.0.1",
        "tcp://:99",
        "tcp://h:0",
        "tcp://h:65536",
        "tcp://h:port",
        "unix:",
    ],
)
def test_invalid_raises(bad):
    with pytest.raises(ValueError):
        parse_endpoint(bad)


def test_resolve_relative_unix_from_base():
    ep = resolve_endpoint("./data/node.sock", base_dir=Path("/opt/bot"))
    assert ep == UnixEndpoint(Path("/opt/bot/data/node.sock"))


def test_resolve_absolute_and_tcp_unchanged():
    assert resolve_endpoint("/tmp/x.sock", Path("/opt")) == UnixEndpoint(Path("/tmp/x.sock"))
    assert resolve_endpoint("tcp://h:1", Path("/opt")) == TcpEndpoint("h", 1)
    assert resolve_endpoint("./x.sock", None) == UnixEndpoint(Path("./x.sock"))


def test_unix_rejected_on_windows(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    for raw in ("./data/node.sock", "unix:/tmp/x.sock", Path("/tmp/x.sock")):
        with pytest.raises(ValueError, match="tcp://127.0.0.1"):
            parse_endpoint(raw)
    # tcp продолжает работать
    assert parse_endpoint("tcp://127.0.0.1:8710") == TcpEndpoint("127.0.0.1", 8710)
