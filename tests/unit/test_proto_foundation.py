from __future__ import annotations

import base64
import hashlib

import pytest

from sa_home_bot.proto.addresses import (
    GRAPH_MEMORY_ADDRESS,
    LLM_ADDRESS,
    MEMORY_ADDRESS,
    NET_ADDRESS,
    TASKS_ADDRESS,
    node_address,
)
from sa_home_bot.proto.chunks import CHUNK_BYTES, download_chunks
from sa_home_bot.proto.emitter import DeferredEmitter
from sa_home_bot.proto.errors import (
    SERVICE_CALL_ERRORS,
    ServiceUnavailableError,
    is_service_unavailable,
)
from sa_home_bot.proto.messages import (
    ERR_INTERNAL,
    ERR_UNAVAILABLE,
    ERR_UNKNOWN_DST,
    Address,
    ProtoError,
)
from tests.unit.support.fakes import FakeNodeLink


def test_chunk_size_is_part_of_the_protocol():
    assert CHUNK_BYTES == 700 * 1024


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ServiceUnavailableError("x"), True),
        (ProtoError(ERR_UNAVAILABLE, "x"), True),
        (ProtoError(ERR_UNKNOWN_DST, "x"), True),
        (ProtoError(ERR_INTERNAL, "x"), False),
        (RuntimeError("x"), False),
    ],
)
def test_is_service_unavailable(error, expected):
    assert is_service_unavailable(error) is expected


def test_service_call_errors_cover_both_classes():
    assert set(SERVICE_CALL_ERRORS) == {ServiceUnavailableError, ProtoError}


def test_service_link_reexports_the_same_unavailable_error():
    from sa_home_bot.bot import service_link

    assert service_link.ServiceUnavailableError is ServiceUnavailableError
    assert issubclass(service_link.ServiceTimeoutError, ServiceUnavailableError)


def test_addresses():
    assert LLM_ADDRESS == Address(node="mycraft", service="llm")
    assert MEMORY_ADDRESS == Address(node="mycraft", service="memory")
    assert GRAPH_MEMORY_ADDRESS == Address(node="mycraft", service="graph_memory")
    assert NET_ADDRESS == Address(node="mycraft", service="net")
    assert TASKS_ADDRESS == Address(node="alfred", service="tasks")
    assert node_address("alfred") == Address(node="alfred", service="node")
    assert node_address(None) is None and node_address("") is None


class ChunkServer(FakeNodeLink):
    def __init__(self, payload: bytes) -> None:
        super().__init__()
        self.payload = payload

    async def command(self, action, args=None, dst=None, *, timeout=None):
        await super().command(action, args, dst)
        offset, length = args["offset"], args["length"]
        data = self.payload[offset : offset + length]
        return {
            "data_b64": base64.b64encode(data).decode(),
            "eof": offset + len(data) >= len(self.payload),
        }


async def test_download_chunks_reassembles_and_passes_base_args():
    payload = bytes(range(256)) * (CHUNK_BYTES // 256 + 100)
    link = ChunkServer(payload)
    dst = Address(node="n", service="s")
    result = await download_chunks(
        link,
        "chunk",
        dst,
        base_args={"session_id": "abc"},
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    assert result == payload
    assert [args for _a, args, _d in link.calls] == [
        {"session_id": "abc", "offset": 0, "length": CHUNK_BYTES},
        {"session_id": "abc", "offset": CHUNK_BYTES, "length": CHUNK_BYTES},
    ]
    assert all(call_dst is dst for _a, _args, call_dst in link.calls)


async def test_download_chunks_rejects_wrong_hash():
    with pytest.raises(ValueError, match="своя ошибка"):
        await download_chunks(
            ChunkServer(b"data"),
            "chunk",
            Address(),
            expected_sha256="0" * 64,
            integrity_error="своя ошибка",
        )


async def test_deferred_emitter_drops_events_until_bound():
    class Server:
        def __init__(self) -> None:
            self.events = []

        async def broadcast_event(self, event_type, data):
            self.events.append((event_type, data))

    emitter = DeferredEmitter()
    await emitter("early", {})
    server = Server()
    emitter.bind(server)
    await emitter("late", {"a": 1})
    assert server.events == [("late", {"a": 1})]
