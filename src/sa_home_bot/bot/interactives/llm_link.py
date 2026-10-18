from __future__ import annotations

import logging
from typing import Any

from sa_home_bot.bot.service_link import ServiceLink
from sa_home_bot.proto.addresses import LLM_ADDRESS, ROLE_DIRECTOR
from sa_home_bot.proto.errors import SERVICE_CALL_ERRORS

log = logging.getLogger(__name__)

ACTION_CHAT = "chat"
LLM_CALL_ERRORS = (*SERVICE_CALL_ERRORS, TimeoutError, OSError)


async def ask_chat(
    node_link: ServiceLink, timeout: float, args: dict[str, Any], *, failure: str
) -> str | None:
    try:
        result = await node_link.command(ACTION_CHAT, args, dst=LLM_ADDRESS, timeout=timeout)
    except LLM_CALL_ERRORS as exc:
        log.warning("interactives: %s (chat=%s): %s", failure, args.get("chat_id"), exc)
        return None
    return str(result.get("response") or "") if isinstance(result, dict) else ""


async def ask_as_director(
    node_link: ServiceLink,
    timeout: float,
    *,
    chat_id: int,
    system: str,
    content: str,
    failure: str,
    **extra: Any,
) -> str | None:
    args: dict[str, Any] = {
        "messages": [{"role": "user", "content": content}],
        "role": ROLE_DIRECTOR,
        "system": system,
        "chat_id": chat_id,
        **extra,
    }
    return await ask_chat(node_link, timeout, args, failure=failure)
