from __future__ import annotations

import asyncio
import ssl
from unittest.mock import AsyncMock, MagicMock

import pytest

from sa_home_bot.vpn_series import mtg_probe


class TestTlsHandshake:
    @pytest.mark.asyncio
    async def test_success(self, monkeypatch):
        mock_reader = MagicMock()
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()

        async def mock_open_connection(*args, **kwargs):
            return mock_reader, mock_writer

        monkeypatch.setattr(asyncio, "open_connection", mock_open_connection)

        error, ms = await mtg_probe.tls_handshake("example.com", 443, "www.microsoft.com", 5.0)

        assert error is None
        assert ms is not None
        assert ms >= 0
        mock_writer.close.assert_called_once()
        mock_writer.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ssl_error(self, monkeypatch):
        async def mock_open_connection(*args, **kwargs):
            raise ssl.SSLError("handshake failure")

        monkeypatch.setattr(asyncio, "open_connection", mock_open_connection)

        error, ms = await mtg_probe.tls_handshake("example.com", 443, "www.microsoft.com", 5.0)

        assert error is not None
        assert "SSLError" in error
        assert ms is None

    @pytest.mark.asyncio
    async def test_timeout(self, monkeypatch):
        async def mock_open_connection(*args, **kwargs):
            raise TimeoutError()

        monkeypatch.setattr(asyncio, "open_connection", mock_open_connection)

        error, ms = await mtg_probe.tls_handshake("example.com", 443, "www.microsoft.com", 5.0)

        assert error is not None
        assert "TimeoutError" in error
        assert ms is None

    @pytest.mark.asyncio
    async def test_os_error(self, monkeypatch):
        async def mock_open_connection(*args, **kwargs):
            raise OSError("connection refused")

        monkeypatch.setattr(asyncio, "open_connection", mock_open_connection)

        error, ms = await mtg_probe.tls_handshake("example.com", 443, "www.microsoft.com", 5.0)

        assert error is not None
        assert "OSError" in error
        assert ms is None


class TestMtgProbe:
    @pytest.mark.asyncio
    async def test_tcp_blocked(self, monkeypatch):
        async def mock_tcp_connect(*args, **kwargs):
            return "OSError: connection refused"

        monkeypatch.setattr(mtg_probe, "tcp_connect", mock_tcp_connect)

        result = await mtg_probe.mtg_probe("example.com", 443, "www.microsoft.com", 5.0)

        assert result.tcp_ok is False
        assert result.tls_ok is None
        assert result.verdict == "tcp_blocked"
        assert result.error == "OSError: connection refused"
        assert result.tls_ms is None

    @pytest.mark.asyncio
    async def test_tls_blocked(self, monkeypatch):
        async def mock_tcp_connect(*args, **kwargs):
            return None

        async def mock_tls_handshake(*args, **kwargs):
            return "SSLError: handshake failure", None

        monkeypatch.setattr(mtg_probe, "tcp_connect", mock_tcp_connect)
        monkeypatch.setattr(mtg_probe, "tls_handshake", mock_tls_handshake)

        result = await mtg_probe.mtg_probe("example.com", 443, "www.microsoft.com", 5.0)

        assert result.tcp_ok is True
        assert result.tls_ok is False
        assert result.verdict == "tls_blocked"
        assert result.error == "SSLError: handshake failure"
        assert result.tls_ms is None

    @pytest.mark.asyncio
    async def test_ok(self, monkeypatch):
        async def mock_tcp_connect(*args, **kwargs):
            return None

        async def mock_tls_handshake(*args, **kwargs):
            return None, 123

        monkeypatch.setattr(mtg_probe, "tcp_connect", mock_tcp_connect)
        monkeypatch.setattr(mtg_probe, "tls_handshake", mock_tls_handshake)

        result = await mtg_probe.mtg_probe("example.com", 443, "www.microsoft.com", 5.0)

        assert result.tcp_ok is True
        assert result.tls_ok is True
        assert result.verdict == "ok"
        assert result.error is None
        assert result.tls_ms == 123
