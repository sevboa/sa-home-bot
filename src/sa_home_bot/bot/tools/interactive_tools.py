from __future__ import annotations

from typing import Any

from sa_home_bot.bot.interactives import acts as interactive_acts
from sa_home_bot.bot.interactives import cabinet as interactive_cabinet
from sa_home_bot.bot.interactives import places as interactive_places
from sa_home_bot.bot.interactives import radio as interactive_radio
from sa_home_bot.bot.interactives import underground as interactive_underground
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec


async def tool_swap_radio(ctx: ToolContext, _args: dict[str, Any]) -> str:
    if ctx.interactives is None:
        return interactive_radio.TOOL_UNAVAILABLE
    return await ctx.interactives.tool_swap_radio(
        ctx.chat_id, ctx.user_id, is_private=ctx.is_private
    )


async def tool_manor_items(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.interactives is None:
        return interactive_radio.TOOL_ITEMS_UNAVAILABLE
    show = args.get("show")
    result = await ctx.interactives.tool_manor_items(
        ctx.chat_id,
        ctx.user_id,
        show=show if isinstance(show, str) else "",
        message_thread_id=ctx.message_thread_id,
        trigger_message_id=ctx.trigger_message_id,
        dialogue_id=ctx.dialogue_id,
    )
    if result.startswith(interactive_radio.TOOL_ITEMS_DRAWING_HEAD):
        ctx.end_turn = True
    return result


async def tool_item_action(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.interactives is None:
        return interactive_radio.TOOL_ACTION_UNAVAILABLE
    item, action = args.get("item"), args.get("action")
    return await ctx.interactives.tool_item_action(
        ctx.chat_id,
        ctx.user_id,
        item=item if isinstance(item, str) else "",
        action=action if isinstance(action, str) else "",
    )


async def tool_take_photo(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.interactives is None:
        return interactive_cabinet.TOOL_PHOTO_UNAVAILABLE
    result = await ctx.interactives.tool_take_photo(
        ctx.chat_id,
        ctx.user_id,
        args,
        message_thread_id=ctx.message_thread_id,
        trigger_message_id=ctx.trigger_message_id,
        dialogue_id=ctx.dialogue_id,
    )
    if result == interactive_cabinet.TOOL_PHOTO_STARTED:
        ctx.end_turn = True
    return result


async def tool_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is None or not canary_ok(ctx.user_id):
        return interactive_places.TOOL_SEARCH_UNAVAILABLE
    where = args.get("where")
    return await ctx.interactives.tool_search(
        ctx.chat_id, ctx.user_id, where if isinstance(where, str) else ""
    )


async def tool_act(ctx: ToolContext, args: dict[str, Any]) -> str:
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is None or not canary_ok(ctx.user_id):
        return interactive_acts.TOOL_ACT_UNAVAILABLE
    return await ctx.interactives.tool_act(ctx.chat_id, ctx.user_id, args.get("action"))


async def tool_walk(ctx: ToolContext, args: dict[str, Any]) -> str:
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is None or not canary_ok(ctx.user_id):
        return interactive_underground.TOOL_WALK_UNAVAILABLE
    return await ctx.interactives.tool_walk(
        ctx.chat_id,
        ctx.user_id,
        args.get("direction"),
        message_thread_id=ctx.message_thread_id,
        trigger_message_id=ctx.trigger_message_id,
    )


async def tool_go(ctx: ToolContext, args: dict[str, Any]) -> str:
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is None or not canary_ok(ctx.user_id):
        return interactive_underground.TOOL_GO_UNAVAILABLE
    return await ctx.interactives.tool_go(
        ctx.chat_id,
        ctx.user_id,
        args.get("place"),
        message_thread_id=ctx.message_thread_id,
        trigger_message_id=ctx.trigger_message_id,
    )


SWAP_RADIO = ToolSpec(
    name="swap_radio",
    handler=tool_swap_radio,
    declaration=interactive_radio.SWAP_RADIO_DECLARATION,
)

TAKE_PHOTO = ToolSpec(
    name="take_photo",
    handler=tool_take_photo,
    declaration=interactive_cabinet.TAKE_PHOTO_DECLARATION,
)

SEARCH = ToolSpec(
    name="search",
    handler=tool_search,
    declaration=interactive_places.SEARCH_DECLARATION,
    telegram_only=True,
)

ACT = ToolSpec(
    name="act",
    handler=tool_act,
    declaration=interactive_acts.ACT_DECLARATION,
    telegram_only=True,
)

WALK = ToolSpec(
    name="walk",
    handler=tool_walk,
    declaration=interactive_underground.WALK_DECLARATION,
    telegram_only=True,
)

GO = ToolSpec(
    name="go",
    handler=tool_go,
    declaration=interactive_underground.GO_DECLARATION,
    telegram_only=True,
)

MANOR_ITEMS = ToolSpec(
    name="manor_items",
    handler=tool_manor_items,
    declaration=interactive_radio.MANOR_ITEMS_DECLARATION,
)

ITEM_ACTION = ToolSpec(
    name="item_action",
    handler=tool_item_action,
    declaration=interactive_radio.ITEM_ACTION_DECLARATION,
)
