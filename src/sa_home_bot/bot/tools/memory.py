from __future__ import annotations

import json
import logging
from typing import Any

from sa_home_bot.bot.tools.core import (
    NO_SWARM_LINK,
    ActionRight,
    ToolContext,
    ToolSpec,
    VariantRights,
    call_service,
    text_arg,
)
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.memory import protocol as memory_protocol
from sa_home_bot.proto.addresses import (
    GRAPH_MEMORY_ADDRESS,
    MEMORY_ADDRESS,
)
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError

log = logging.getLogger(__name__)


async def tool_memory(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.node_link is None:
        return NO_SWARM_LINK
    if ctx.chat_id is None:
        return "недоступно: память привязана к разговору, а его сейчас нет"
    action = text_arg(args, "action")
    refusal = _MEMORY_VARIANTS.refusal_for(ctx.subscription, action)
    if refusal is not None:
        return refusal

    payload: dict[str, Any] = {"chat_id": ctx.chat_id}
    if action == memory_protocol.ACTION_REMEMBER:
        text = text_arg(args, "text")
        if not text:
            return "ошибка: не сказано, что запомнить (text)"
        payload["text"] = text
    elif action == memory_protocol.ACTION_RECALL:
        query = text_arg(args, "query")
        if not query:
            return "ошибка: не сказано, о чём вспомнить (query)"
        payload["query"] = query
    elif action == memory_protocol.ACTION_FORGET:
        raw_id = args.get("id")
        if raw_id is None:
            return "ошибка: не указан номер факта (id) — возьми его из recall"
        payload["id"] = raw_id

    result = await call_service(
        ctx, action, payload, MEMORY_ADDRESS, unavailable_subject="память не отвечает"
    )
    if isinstance(result, str):
        return result
    if action == memory_protocol.ACTION_REMEMBER:
        await piggyback_graph_episode(
            ctx,
            payload["text"],
            ctx.chat_id,
            source=graph_memory_protocol.EPISODE_SOURCE_MEMORY_FACT,
        )
    if action == memory_protocol.ACTION_RECALL and not result.get("facts"):
        return "в памяти про это ничего нет"
    return json.dumps(result, ensure_ascii=False)


_GRAPH_EPISODE_MAX_CHARS = 400


async def piggyback_graph_episode(
    ctx: ToolContext, text: str, chat_id: int, *, source: str
) -> None:
    if ctx.node_link is None:
        return
    if len(text) > _GRAPH_EPISODE_MAX_CHARS:
        text = text[: _GRAPH_EPISODE_MAX_CHARS - 1] + "…"
    dst = GRAPH_MEMORY_ADDRESS
    try:
        await ctx.node_link.command(
            graph_memory_protocol.ACTION_ADD_EPISODE,
            {"text": text, "chat_id": chat_id, "source": source},
            dst=dst,
            timeout=2.0,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError, OSError) as exc:
        log.debug("graph_memory недоступна, эпизод (%s) не задублирован: %s", source, exc)


_MEMORY_VARIANTS = VariantRights(
    param="action",
    rights=(
        (memory_protocol.ACTION_RECALL, ActionRight("recall", memory_protocol.SERVICE_NAME)),
        (memory_protocol.ACTION_REMEMBER, ActionRight("remember", memory_protocol.SERVICE_NAME)),
        (memory_protocol.ACTION_FORGET, ActionRight("forget", memory_protocol.SERVICE_NAME)),
    ),
)

_DECL_MEMORY: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "memory",
        "description": (
            "Твоя долгая память об ЭТОМ разговоре и его людях — то, что иначе "
            "забудется, когда тред закончится: привычки и предпочтения "
            "(«качаем в такую-то папку», «смотрим в такой-то озвучке»), "
            "договорённости, имена и роли машин, всё названное «запомни».\n"
            "remember — записать ОДНУ мысль своими словами, коротко и так, "
            "чтобы через месяц было понятно без разговора вокруг. ВАЖНО: "
            "когда факт потом подмешивается тебе перед ответом, это звучит "
            "как «то, что ты САМ помнишь» — то есть от ТВОЕГО (Альфреда) "
            "лица. Поэтому «я»/«моё» в тексте факта пиши только про себя "
            "самого — а факты о собеседнике и других людях формулируй в "
            "третьем лице, называя их по имени («Наташа — жена Алексея», "
            "НЕ «Наташа — моя жена»), иначе при следующем чтении факт "
            "прочитается как относящийся к тебе самому. Не пересказывай "
            "беседу и не записывай мелочи вроде «спросил погоду» — память "
            "не дневник. Кто человек — имя, ник, id, пол, день рождения, "
            "город — сюда не пиши: это карточка человека, note_person.\n"
            "recall — поискать в памяти словами. Подходящее и так "
            "подмешивается тебе перед ответом, так что зови recall, только "
            "если нужно копнуть глубже, чем уже дали.\n"
            "forget — стереть факт по id из recall (когда он устарел или "
            "человек просит забыть).\n"
            "Память у каждого разговора своя, чужую ты не видишь — это не "
            "ограничение, которое надо обходить, а как оно устроено."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [v for v, _ in _MEMORY_VARIANTS.rights],
                    "description": "recall — вспомнить; remember — запомнить; forget — забыть",
                },
                "text": {"type": "string", "description": "remember: сам факт, одной мыслью"},
                "query": {"type": "string", "description": "recall: о чём вспомнить, словами"},
                "id": {"type": "integer", "description": "forget: номер факта из recall"},
            },
            "required": ["action"],
        },
    },
}


MEMORY = ToolSpec(
    name="memory", handler=tool_memory, declaration=_DECL_MEMORY, variants=_MEMORY_VARIANTS
)
