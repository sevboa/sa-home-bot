from __future__ import annotations

import json
from typing import Any

from sa_home_bot.bot.tools.core import NO_SWARM_LINK, ActionRight, ToolContext, ToolSpec, text_arg
from sa_home_bot.bot.tools.memory import piggyback_graph_episode
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.net import protocol as net_protocol
from sa_home_bot.proto.addresses import (
    NET_ADDRESS,
)
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError

# --- web_search: интернет через свой SearXNG (LLM_INTEGRATION_PLAN.md §9) ---


def _web_search_episode_text(query: str, results: list[dict[str, Any]]) -> str:
    """Заголовки+выдержки в один текст эпизода — конечную обрезку до
    _GRAPH_EPISODE_MAX_CHARS делает сам piggyback_graph_episode."""
    parts = [f"Поиск «{query}»:"]
    for item in results:
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if title and snippet:
            parts.append(f"{title} — {snippet}")
        elif title:
            parts.append(title)
    return " ".join(parts)


# Запас сверх net.request_timeout_s на дорогу alfred ↔ mycraft и разбор
# выдачи — чтобы ответ службы успел доехать, а не упасть в таймаут у финиша.
_WEB_SEARCH_TIMEOUT_MARGIN_S = 5.0


async def tool_web_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return NO_SWARM_LINK
    query = text_arg(args, "query")
    if not query:
        return "ошибка: не указан поисковый запрос"
    dst = NET_ADDRESS
    try:
        # Ждём дольше, чем net сам ждёт SearXNG: без явного таймаута
        # действовали 10 с ProtoClient по умолчанию, а поиску отведено 20
        # (живая находка 2026-09-25 — медленный brave обрывал ответ целиком).
        result = await ctx.node_link.command(
            net_protocol.ACTION_SEARCH,
            {"query": query},
            dst=dst,
            timeout=ctx.settings.net.request_timeout_s + _WEB_SEARCH_TIMEOUT_MARGIN_S,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
        # §7.3: недоступный поисковик — обычный результат тула, персонаж сам
        # решит, как об этом сказать; цикл tool-calling не роняем.
        return f"недоступно: поиск не работает ({exc})"
    results = result.get("results") or []
    if not results:
        return f"по запросу «{query}» ничего не нашлось"
    if ctx.chat_id is not None:
        # Этап 42.3: сырая выдача поиска — тоже эпизод графа, тем же
        # best-effort приёмом, что у remember/look_at_photo выше.
        await piggyback_graph_episode(
            ctx,
            _web_search_episode_text(query, results),
            ctx.chat_id,
            source=graph_memory_protocol.EPISODE_SOURCE_WEB_SEARCH,
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
