"""Пара клиент↔сервер протокола v0 по TCP c auth-токеном."""

import asyncio
import socket

import pytest

from sa_home_bot.proto.client import ProtoClient
from sa_home_bot.proto.endpoints import TcpEndpoint
from sa_home_bot.proto.messages import (
    ERR_UNAUTHORIZED,
    ActionSpec,
    ProtoError,
    ServiceDescription,
    ServiceInfo,
    decode,
    encode,
    make_request,
)
from sa_home_bot.proto.server import ProtoServer

TOKEN = "swarm-secret"


class FakeService:
    def describe(self) -> ServiceDescription:
        return ServiceDescription(
            info=ServiceInfo(node="winpc", service="monitor", version="0.11.0"),
            capabilities=("temperature",),
            actions=(ActionSpec(id="scan_now", title="Запустить скан"),),
        )

    async def get_state(self) -> dict:
        return {"status": "ok"}

    async def run_command(self, action: str, args: dict) -> dict:
        return {"accepted": True}


@pytest.fixture
async def tcp_server():
    # Порт 0 — свободный порт выберет ОС; реальный отдаёт server.endpoint.
    server = ProtoServer(TcpEndpoint("127.0.0.1", 0), FakeService(), token=TOKEN)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_tcp_requires_token_at_construction():
    with pytest.raises(ValueError):
        ProtoServer(TcpEndpoint("127.0.0.1", 0), FakeService())


async def test_tcp_roundtrip_with_token(tcp_server):
    client = ProtoClient(tcp_server.endpoint, token=TOKEN, timeout=5.0)
    await client.connect()
    try:
        info = await client.hello()
        assert (info.node, info.service) == ("winpc", "monitor")
        assert (await client.get_state())["status"] == "ok"
        assert (await client.command("scan_now"))["accepted"] is True
    finally:
        await client.close()


async def test_wrong_token_rejected_and_closed(tcp_server):
    client = ProtoClient(tcp_server.endpoint, token="wrong", timeout=5.0)
    with pytest.raises(ProtoError) as exc_info:
        await client.connect()
    assert exc_info.value.code == ERR_UNAUTHORIZED
    assert not client.connected


async def test_request_before_auth_rejected_and_closed(tcp_server):
    ep = tcp_server.endpoint
    reader, writer = await asyncio.open_connection(ep.host, ep.port)
    try:
        writer.write(encode(make_request("get_state")))
        await writer.drain()
        response = decode(await reader.readline())
        assert response.ok is False
        assert response.error_code() == ERR_UNAUTHORIZED
        # сервер закрывает соединение после unauthorized
        assert await reader.readline() == b""
    finally:
        writer.close()


async def test_events_only_to_authenticated(tcp_server):
    ep = tcp_server.endpoint
    got_event = asyncio.Event()

    async def on_event(env):
        got_event.set()

    authed = ProtoClient(ep, token=TOKEN, on_event=on_event, timeout=5.0)
    await authed.connect()
    # Второе соединение висит без auth — событий получать не должно.
    _, raw_writer = await asyncio.open_connection(ep.host, ep.port)
    try:
        delivered = await tcp_server.broadcast_event("service_started", {"name": "x"})
        assert delivered == 1
        await asyncio.wait_for(got_event.wait(), timeout=5.0)
    finally:
        raw_writer.close()
        await authed.close()


async def test_dual_listen_unix_and_tcp():
    """Нода слушает unix (локальные, без auth) и tcp (пиры, с auth) сразу."""
    import shutil
    import tempfile
    from pathlib import Path

    tmpdir = Path(tempfile.mkdtemp(prefix="sa-dual-"))
    server = ProtoServer(
        [tmpdir / "node.sock", TcpEndpoint("127.0.0.1", 0)], FakeService(), token=TOKEN
    )
    await server.start()
    try:
        unix_ep, tcp_ep = server.endpoints
        got = asyncio.Event()

        async def on_event(env):
            got.set()

        # Локальный клиент по unix — токен не нужен.
        local = ProtoClient(unix_ep, on_event=on_event, timeout=5.0)
        await local.connect()
        assert (await local.hello()).node == "winpc"
        # Пир по tcp — с токеном.
        peer = ProtoClient(tcp_ep, token=TOKEN, timeout=5.0)
        await peer.connect()
        assert (await peer.get_state())["status"] == "ok"
        # Событие доходит обоим.
        assert await server.broadcast_event("x", {}) == 2
        await asyncio.wait_for(got.wait(), timeout=5.0)
        # На tcp-слушателе auth обязателен даже при живом unix-слушателе.
        reader, writer = await asyncio.open_connection(tcp_ep.host, tcp_ep.port)
        writer.write(encode(make_request("get_state")))
        await writer.drain()
        assert decode(await reader.readline()).error_code() == ERR_UNAUTHORIZED
        writer.close()
        await local.close()
        await peer.close()
    finally:
        await server.stop()
        shutil.rmtree(tmpdir, ignore_errors=True)


async def test_repeated_auth_is_ok(tcp_server):
    client = ProtoClient(tcp_server.endpoint, token=TOKEN, timeout=5.0)
    await client.connect()
    try:
        assert (await client.request("auth", {"token": TOKEN}))["authenticated"] is True
    finally:
        await client.close()


# --- TCP keepalive: обнаружение молча пропавшего пира (2026-07-20) ---


async def test_tcp_connect_enables_keepalive(tcp_server):
    """Без этого сон/обрыв сети у пира не замечается годами (ОС-дефолт) —
    PeerLink.alive врал бы "жив" бесконечно (живой баг, см. proto/client.py)."""
    client = ProtoClient(tcp_server.endpoint, token=TOKEN, timeout=5.0)
    await client.connect()
    try:
        raw_sock = client._writer.get_extra_info("socket")
        assert raw_sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
    finally:
        await client.close()


async def test_unix_connect_does_not_touch_keepalive(tmp_path):
    """Unix-сокеты не роняют connect() попыткой выставить TCP-опции."""
    from sa_home_bot.proto.messages import ServiceDescription, ServiceInfo

    class Svc:
        def describe(self):
            return ServiceDescription(info=ServiceInfo(node="n", service="s", version="0.1"))

        async def get_state(self):
            return {}

        async def run_command(self, action, args):
            return {}

    sock_path = tmp_path / "u.sock"
    server = ProtoServer(sock_path, Svc())
    await server.start()
    try:
        client = ProtoClient(sock_path, timeout=5.0)
        await client.connect()  # не должно бросить исключение
        try:
            assert (await client.hello()).node == "n"
        finally:
            await client.close()
    finally:
        await server.stop()
