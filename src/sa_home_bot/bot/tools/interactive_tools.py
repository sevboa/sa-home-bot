from __future__ import annotations

from typing import Any

from sa_home_bot.bot.interactives import acts as interactive_acts
from sa_home_bot.bot.interactives import cabinet as interactive_cabinet
from sa_home_bot.bot.interactives import places as interactive_places
from sa_home_bot.bot.interactives import radio as interactive_radio
from sa_home_bot.bot.interactives import underground as interactive_underground
from sa_home_bot.bot.tools.core import ToolContext, ToolSpec


async def tool_swap_radio(ctx: ToolContext, _args: dict[str, Any]) -> str:
    """Устройство связи Альфреда (Этап 47, bot/interactives/radio.py). Один
    тул на обе стороны: до завершения интерактива «Проклятый передатчик»
    он лишь запускает/продвигает сцену (в финале — форма замены), после —
    переключатель старый/новый передатчик. Решает кнопка формы, не модель.
    Службе tasks формы показать некому — честный отказ."""
    if ctx.interactives is None:
        return interactive_radio.TOOL_UNAVAILABLE
    return await ctx.interactives.tool_swap_radio(
        ctx.chat_id, ctx.user_id, is_private=ctx.is_private
    )


async def tool_manor_items(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Особенные вещи поместья (Этап 49.3.6, bot/interactives/items.py):
    опись — модели, она отвечает своими словами; ``show`` — карточка вещи
    уходит в чат сама."""
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
    # Портрет вещи ещё рисуется — Альфред молчит до карточки, как со снимком:
    # «сейчас подготовлю описание» ломало четвёртую стену (живая находка
    # 2026-10-15).
    if result.startswith(interactive_radio.TOOL_ITEMS_DRAWING_HEAD):
        ctx.end_turn = True
    return result


async def tool_item_action(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Действие с особенной вещью поместья (Этап 49.3.7): форма с кнопками
    из данных действия вида (bot/interactives/items.py); делает кнопка."""
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
    """Снимок кабинета Альфреда (Этап 49.2, bot/interactives/cabinet.py) —
    вся логика в Interactives.tool_take_photo. Рисуется в фоне и приходит
    сам; у службы tasks интерактивов нет — честный отказ."""
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
    # Снимок пошёл рисоваться — Альфред молчит, пока он не готов: описание
    # «до снимка» выходило выдуманным (живая находка 2026-10-02).
    if result == interactive_cabinet.TOOL_PHOTO_STARTED:
        ctx.end_turn = True
    return result


async def tool_search(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Порыскать в месте, где стоит Альфред (Этап 59.1, только канарейкам):
    итог решает код по таблице находок места — Interactives.tool_search."""
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is None or not canary_ok(ctx.user_id):
        return interactive_places.TOOL_SEARCH_UNAVAILABLE
    where = args.get("where")
    return await ctx.interactives.tool_search(
        ctx.chat_id, ctx.user_id, where if isinstance(where, str) else ""
    )


async def tool_act(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Физическое действие Альфреда (Этап 59.7, только канарейкам): исход даёт
    Ведущий синхронно — Interactives.tool_act."""
    canary_ok = getattr(ctx.interactives, "canary_ok", None)
    if canary_ok is None or not canary_ok(ctx.user_id):
        return interactive_acts.TOOL_ACT_UNAVAILABLE
    return await ctx.interactives.tool_act(ctx.chat_id, ctx.user_id, args.get("action"))


async def tool_walk(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Шаг по катакомбам (Этап 59.4, только канарейкам): топологию решает код,
    описание нового места присылает Ведущий — Interactives.tool_walk."""
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
    """Быстрый переход знакомой дорогой (Этап 59.5, только канарейкам) —
    Interactives.tool_go."""
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
