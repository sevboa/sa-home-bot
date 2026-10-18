from __future__ import annotations

import json
from typing import Any

from sa_home_bot.bot.tools.core import NO_SWARM_LINK, ActionRight, ToolContext, ToolSpec, text_arg
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.graph_memory.client import add_episode
from sa_home_bot.net import protocol as net_protocol
from sa_home_bot.proto.addresses import (
    NET_ADDRESS,
)
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError


def _web_search_episode_text(query: str, results: list[dict[str, Any]]) -> str:
    parts = [f"Поиск «{query}»:"]
    for item in results:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if title and snippet:
            parts.append(f"{title} — {snippet}")
        elif title:
            parts.append(title)
    return " ".join(parts)


_WEB_SEARCH_TIMEOUT_MARGIN_S = 5.0


async def tool_web_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return NO_SWARM_LINK
    query = text_arg(args, "query")
    if not query:
        return "ошибка: не указан поисковый запрос"
    dst = NET_ADDRESS
    try:
        result = await ctx.node_link.command(
            net_protocol.ACTION_SEARCH,
            {"query": query},
            dst=dst,
            timeout=ctx.settings.net.request_timeout_s + _WEB_SEARCH_TIMEOUT_MARGIN_S,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
        return f"недоступно: поиск не работает ({exc})"
    results = result.get("results") or []
    if not results:
        return f"по запросу «{query}» ничего не нашлось"
    if ctx.chat_id is not None and ctx.node_link is not None:
        await add_episode(
            ctx.node_link,
            ctx.chat_id,
            _web_search_episode_text(query, results),
            graph_memory_protocol.EPISODE_SOURCE_WEB_SEARCH,
        )
    return json.dumps(result, ensure_ascii=False)


_DECL_WEB_SEARCH: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Поискать в интернете. Используй, когда ответа нет в разговоре и "
            "он может не совпадать с тем, что ты помнишь: свежие события, "
            "новости, цены, факты после твоего обучения, а также всё, в чём "
            "не уверен — лучше поискать, чем придумать. Возвращает заголовки, "
            "ссылки и короткие выдержки; самих страниц по ссылкам ты не "
            "видишь, поэтому отвечай по выдержкам и не выдумывай деталей, "
            "которых в них нет."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос обычными словами",
                }
            },
            "required": ["query"],
        },
    },
}


WEB_SEARCH = ToolSpec(
    name="web_search",
    handler=tool_web_search,
    declaration=_DECL_WEB_SEARCH,
    requires=ActionRight(net_protocol.ACTION_SEARCH, net_protocol.SERVICE_NAME),
)
