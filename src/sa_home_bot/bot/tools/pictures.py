from __future__ import annotations

import base64
from typing import Any

from sa_home_bot.bot import (
    image_tools,
)
from sa_home_bot.bot.tools.core import ActionRight, ToolContext, ToolSpec
from sa_home_bot.graph_memory import protocol as graph_memory_protocol
from sa_home_bot.graph_memory.client import add_episode
from sa_home_bot.proto.addresses import (
    LLM_ADDRESS,
    LLM_SERVICE,
)
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError

ACTION_LOOK_AT_PHOTO = "look_at_photo"


async def tool_generate_image(ctx: ToolContext, args: dict[str, Any]) -> str:
    async def remember(text: str) -> None:
        if ctx.chat_id is None or ctx.node_link is None:
            return
        await add_episode(
            ctx.node_link, ctx.chat_id, text, graph_memory_protocol.EPISODE_SOURCE_IMAGE
        )

    return await image_tools.generate(ctx, args, remember=remember)


async def tool_find_image(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await image_tools.find(ctx, args)


_DECL_LOOK_AT_PHOTO: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "look_at_photo",
        "description": (
            "Посмотреть на картинку: фото, которое собеседник прислал РАНЕЕ в "
            "этом разговоре (whose=guest), или твою собственную — снимок кабинета, "
            "нарисованную картинку, карточку вещи (whose=mine). Фото из ТЕКУЩЕГО "
            "сообщения ты и так видишь. Своих картинок ты не видишь, пока не "
            "посмотришь этим тулом: спрашивают, что на твоём снимке или "
            "рисунке, — сначала посмотри, не выдумывай. Если собеседник ответил "
            "на твою картинку, тул сам возьмёт её."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Что именно нужно рассмотреть или уточнить на картинке",
                },
                "whose": {
                    "type": "string",
                    "enum": ["guest", "mine"],
                    "description": "guest — фото собеседника (по умолчанию), mine — твоя картинка",
                },
                "image_id": {
                    "type": "integer",
                    "description": "Номер твоей картинки (#N), если речь о конкретной",
                },
            },
            "required": ["question"],
        },
    },
}


async def _own_picture(ctx: ToolContext, args: dict[str, Any]) -> dict | str | None:
    assert ctx.store is not None and ctx.chat_id is not None
    whose = args.get("whose")
    image_id = args.get("image_id")
    if isinstance(image_id, bool) or not isinstance(image_id, int):
        image_id = None
    if whose != "guest" and image_id is None and ctx.reply_to_message_id is not None:
        replied = await ctx.store.alfred_image(ctx.chat_id, message_id=ctx.reply_to_message_id)
        if replied is not None:
            return replied
    if whose != "mine" and image_id is None:
        return None
    found = await ctx.store.alfred_image(ctx.chat_id, image_id=image_id)
    if found is None:
        return (
            f"картинки #{image_id} в этом чате нет"
            if image_id is not None
            else "ты ещё ничего не присылал в этот чат — смотреть не на что"
        )
    return found


async def tool_look_at_photo(ctx: ToolContext, args: dict[str, Any]) -> str:
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return "ошибка: не указан вопрос про фото (question)"
    if ctx.store is None or ctx.chat_id is None:
        return "ошибка: сейчас не могу вернуться к фото (нет доступа к истории треда)"
    request: dict[str, Any] = {"question": question, "chat_id": ctx.chat_id}
    mine = await _own_picture(ctx, args)
    if isinstance(mine, str):
        return mine
    if mine is not None:
        request["photo_key"] = f"img-{mine['id']}"
        request["raw_image"] = base64.b64encode(
            image_tools.upscale_png(mine["png"], ctx.settings.llm.imagegen_display_px)
        ).decode()
    else:
        if ctx.dialogue_id is None:
            return "ошибка: сейчас не могу вернуться к фото (нет доступа к истории треда)"
        turn = await ctx.store.latest_photo_turn(ctx.chat_id, ctx.dialogue_id)
        if turn is None:
            return "в этом разговоре фото не найдено — переспроси, если оно точно было"
        request["photo_key"] = turn["photo_path"]
    if ctx.node_link is None:
        return "ошибка: сейчас не могу связаться с Альфредом, чтобы посмотреть на фото"
    try:
        result = await ctx.node_link.command(
            ACTION_LOOK_AT_PHOTO,
            request,
            dst=LLM_ADDRESS,
            timeout=ctx.settings.llm.request_timeout_s,
        )
    except (ServiceUnavailableError, ProtoError, TimeoutError) as exc:
        return f"не получилось рассмотреть фото ещё раз: {exc}"
    response = str(result.get("response", "")).strip()
    if response and ctx.node_link is not None:
        await add_episode(
            ctx.node_link,
            ctx.chat_id,
            f"Фото, вопрос «{question}»: {response}",
            graph_memory_protocol.EPISODE_SOURCE_LOOK_AT_PHOTO,
        )
    return response or "не удалось разглядеть — возможно, стоит переспросить"


LOOK_AT_PHOTO = ToolSpec(
    name="look_at_photo",
    handler=tool_look_at_photo,
    declaration=_DECL_LOOK_AT_PHOTO,
    telegram_only=True,
)

GENERATE_IMAGE = ToolSpec(
    name="generate_image",
    handler=tool_generate_image,
    declaration=image_tools.GENERATE_IMAGE_DECLARATION,
    requires=ActionRight(image_tools.ACTION_GENERATE_IMAGE, LLM_SERVICE),
)

FIND_IMAGE = ToolSpec(
    name="find_image", handler=tool_find_image, declaration=image_tools.FIND_IMAGE_DECLARATION
)
