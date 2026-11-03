from __future__ import annotations

import asyncio
import socket
import ssl
from unittest.mock import MagicMock

import pytest

from sa_home_bot.vpn_series.sni_probe import SniVerdict, probe_many, probe_sni


class FakeWriter:
    def __init__(self, ssl_object: MagicMock | None = None):
        self._ssl_object = ssl_object
        self.closed = False

    def get_extra_info(self, name: str):
        if name == "ssl_object":
            return self._ssl_object
        return None

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class FakeSSLObject:
    def __init__(self, version: str = "TLSv1.3", alpn: str | None = "h2"):
        self._version = version
        self._alpn = alpn

    def version(self):
        return self._version

    def selected_alpn_protocol(self):
        return self._alpn


@pytest.mark.parametrize(
    "getaddrinfo_error,open_connection_error,ssl_version,alpn,expected_verdict,expected_error_contains",
    [
        (
            socket.gaierror("Name or service not known"),
            None,
            None,
            None,
            SniVerdict.dns_fail,
            "gaierror",
        ),
        (None, TimeoutError(), None, None, SniVerdict.tcp_fail, "таймаут"),
        (
            None,
            OSError("Connection refused"),
            None,
            None,
            SniVerdict.tcp_fail,
            "Connection refused",
        ),
        (None, ssl.SSLError("bad handshake"), None, None, SniVerdict.tls_fail, "SSLError"),
        (None, None, "TLSv1.2", "h2", SniVerdict.no_tls13, "TLSv1.2"),
        (None, None, "TLSv1.3", "http/1.1", SniVerdict.no_h2, "http/1.1"),
        (None, None, "TLSv1.3", "h2", SniVerdict.ok, None),
    ],
)
async def test_probe_sni_verdicts(
    monkeypatch,
    getaddrinfo_error,
    open_connection_error,
    ssl_version,
    alpn,
    expected_verdict,
    expected_error_contains,
):
    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        if getaddrinfo_error:
            raise getaddrinfo_error
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        if open_connection_error:
            raise open_connection_error
        ssl_obj = FakeSSLObject(version=ssl_version, alpn=alpn)
        writer = FakeWriter(ssl_object=ssl_obj)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await probe_sni("example.com", timeout=5.0)

    assert result.domain == "example.com"
    assert result.verdict == expected_verdict
    if expected_verdict == SniVerdict.ok:
        assert result.ms is not None
        assert result.error is None
    else:
        assert result.error is not None
        if expected_error_contains:
            assert expected_error_contains in result.error


async def test_probe_sni_no_ssl_object(monkeypatch):
    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        writer = FakeWriter(ssl_object=None)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await probe_sni("example.com", timeout=5.0)

    assert result.verdict == SniVerdict.tls_fail
    assert "нет ssl_object" in result.error


async def test_probe_many_preserves_order(monkeypatch):
    call_order = []

    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        call_order.append(("getaddrinfo", domain))
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        call_order.append(("open_connection", domain))
        ssl_obj = FakeSSLObject()
        writer = FakeWriter(ssl_object=ssl_obj)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    domains = ["a.example.com", "b.example.com", "c.example.com"]
    results = await probe_many(domains, concurrency=2, timeout=5.0)

    assert [r.domain for r in results] == domains
    assert all(r.verdict == SniVerdict.ok for r in results)


async def test_probe_many_concurrency_limit(monkeypatch):
    active = 0
    max_active = 0

    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        ssl_obj = FakeSSLObject()
        writer = FakeWriter(ssl_object=ssl_obj)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    domains = [f"d{i}.example.com" for i in range(6)]
    await probe_many(domains, concurrency=2, timeout=5.0)

    assert max_active <= 2


async def test_probe_sni_measures_time(monkeypatch):
    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        await asyncio.sleep(0.01)
        ssl_obj = FakeSSLObject()
        writer = FakeWriter(ssl_object=ssl_obj)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await probe_sni("example.com", timeout=5.0)

    assert result.verdict == SniVerdict.ok
    assert result.ms is not None
    assert result.ms >= 10


async def test_probe_sni_writer_closed_on_error(monkeypatch):
    closed = False

    class TrackingWriter(FakeWriter):
        def close(self):
            nonlocal closed
            closed = True
            super().close()

    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        ssl_obj = FakeSSLObject(version="TLSv1.2")
        writer = TrackingWriter(ssl_object=ssl_obj)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await probe_sni("example.com", timeout=5.0)

    assert result.verdict == SniVerdict.no_tls13
    assert closed


async def test_probe_sni_custom_port(monkeypatch):
    captured_port = None

    async def fake_getaddrinfo(domain, port, type=socket.SOCK_STREAM):
        nonlocal captured_port
        captured_port = port
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("1.2.3.4", port))]

    async def fake_open_connection(domain, port, ssl=None, server_hostname=None):
        ssl_obj = FakeSSLObject()
        writer = FakeWriter(ssl_object=ssl_obj)
        reader = asyncio.StreamReader()
        return reader, writer

    monkeypatch.setattr(asyncio.get_event_loop(), "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    await probe_sni("example.com", port=8443, timeout=5.0)

    assert captured_port == 8443
