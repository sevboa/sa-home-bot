from __future__ import annotations

from functools import partial

from sa_home_bot.bot import tools
from tests.unit.tools_support import (
    ADMIN,
    declared_enum,
    swarm_link,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

_dismiss_enum = partial(declared_enum, "dismiss", "mode")


async def test_dismiss_only_records_intent(store):
    box = tools.DismissalBox()
    link = swarm_link()
    result = await tools.tool_dismiss(
        _ctx(store, node_link=link, subscription=ADMIN, dismissal=box), {"mode": "off"}
    )
    assert box.mode == "off"
    assert link.commands == []
    assert result.startswith("принято")


async def test_dismiss_without_box_is_honest(store):
    result = await tools.tool_dismiss(
        _ctx(store, subscription=ADMIN, dismissal=None), {"mode": "off"}
    )
    assert result.startswith("недоступно")


async def test_recall_tool_result_returns_cached_full_text(store):
    ctx = _ctx(store, subscription=ADMIN)
    ctx.tool_result_cache["abc123"] = "полный несокращённый текст результата"
    result = await tools.tool_recall_tool_result(ctx, {"id": "abc123"})
    assert result == "полный несокращённый текст результата"


async def test_recall_tool_result_unknown_id_is_honest(store):
    result = await tools.tool_recall_tool_result(
        _ctx(store, subscription=ADMIN), {"id": "нет-такого"}
    )
    assert result.startswith("ошибка")


async def test_recall_tool_result_without_id(store):
    result = await tools.tool_recall_tool_result(_ctx(store, subscription=ADMIN), {})
    assert result.startswith("ошибка")
