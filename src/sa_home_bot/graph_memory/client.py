from __future__ import annotations

import logging
from typing import Any

from sa_home_bot.graph_memory import protocol
from sa_home_bot.proto.addresses import GRAPH_MEMORY_ADDRESS
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)

ELLIPSIS = "…"
SWALLOWED_ERRORS = (ServiceUnavailableError, ProtoError, TimeoutError, OSError)


def clip_episode(text: str) -> str:
    if len(text) > protocol.MAX_EPISODE_CHARS:
        return text[: protocol.MAX_EPISODE_CHARS - 1] + ELLIPSIS
    return text


async def add_episode(node_link: Any, chat_id: int, text: str, source: str) -> None:
    try:
        await node_link.command(
            protocol.ACTION_ADD_EPISODE,
            {"text": clip_episode(text), "chat_id": chat_id, "source": source},
            dst=GRAPH_MEMORY_ADDRESS,
            timeout=protocol.EPISODE_TIMEOUT_S,
        )
    except SWALLOWED_ERRORS as exc:
        log.debug("graph_memory: эпизод (%s) не принят: %s", source, exc)
