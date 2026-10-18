from __future__ import annotations

import pytest

from sa_home_bot.graph_memory import client, protocol
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ERR_INTERNAL, ProtoError


class EpisodeLink:
    def __init__(self, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, object, float]] = []
        self._raises = raises

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append((action, args, dst, timeout))
        if self._raises is not None:
            raise self._raises
        return {"queued": True, "id": 1}


async def test_add_episode_sends_text_to_graph_memory_with_short_timeout():
    link = EpisodeLink()

    await client.add_episode(
        link, 777, "Наташа — жена Алексея", protocol.EPISODE_SOURCE_MEMORY_FACT
    )

    action, args, dst, timeout = link.calls[0]
    assert action == protocol.ACTION_ADD_EPISODE
    assert args == {"text": "Наташа — жена Алексея", "chat_id": 777, "source": "memory_fact"}
    assert (dst.node, dst.service) == (protocol.NODE_ID, protocol.SERVICE_NAME)
    assert timeout == protocol.EPISODE_TIMEOUT_S


async def test_add_episode_clips_text_to_the_service_limit():
    link = EpisodeLink()

    await client.add_episode(link, 1, "я" * 1000, "dialogue_turn")

    sent = link.calls[0][1]["text"]
    assert len(sent) == protocol.MAX_EPISODE_CHARS
    assert sent.endswith(client.ELLIPSIS)


@pytest.mark.parametrize(
    "error",
    [
        ServiceUnavailableError("mycraft спит"),
        ProtoError(ERR_INTERNAL, "x"),
        TimeoutError(),
        OSError(),
    ],
)
async def test_add_episode_swallows_unavailable_graph_memory(error):
    await client.add_episode(EpisodeLink(raises=error), 1, "привет", "dialogue_turn")
