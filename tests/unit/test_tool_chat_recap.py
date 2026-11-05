from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sa_home_bot.bot import tools as ai_tools
from sa_home_bot.bot.tools.chat_recap import (
    CHAT_RECAP,
    EMPTY_RECORD,
    NO_RECORD,
    NO_SWARM_LINK,
    RETELL_FAILED,
)
from sa_home_bot.bot.tools.core import ToolContext
from sa_home_bot.config import Settings
from sa_home_bot.memory import protocol as memory_protocol
from sa_home_bot.subscriptions.models import Subscription


class FakeNodeLink:
    def __init__(self, response="итог"):
        self.calls = []
        self.response = response

    async def command(self, action, args=None, dst=None, *, timeout=None):
        self.calls.append(args)
        return {"response": self.response}


def _ctx(store, link, chat_id=-100, trigger=None):
    return ToolContext(
        chat_id=chat_id,
        dialogue_id=None,
        trigger_message_id=trigger,
        settings=Settings(),
        node_link=link,
        store=store,
    )


async def _fill(store, chat_id=-100, count=5):
    for i in range(1, count + 1):
        await store.record_group_message(
            chat_id,
            i,
            created_at=datetime(2026, 11, 5, 12, tzinfo=UTC),
            user_id=1,
            user_name="Анна",
            content=f"реплика {i}",
            reply_to=None,
            from_alfred=False,
            keep=1000,
        )


async def test_chat_recap_basic(store):
    link = FakeNodeLink("итог")
    await _fill(store)
    ctx = _ctx(store, link)
    result = await CHAT_RECAP.handler(ctx, {})
    assert "Пересказ 5 последних сообщений группы (всего в записи 5)" in result
    assert "итог" in result


async def test_chat_recap_cached_on_second_call(store):
    link = FakeNodeLink("итог")
    await _fill(store)
    ctx = _ctx(store, link)
    await CHAT_RECAP.handler(ctx, {})
    await CHAT_RECAP.handler(ctx, {})
    assert len(link.calls) == 1


async def test_chat_recap_trigger_excludes_message(store):
    link = FakeNodeLink("итог")
    await _fill(store, count=5)
    ctx = _ctx(store, link, trigger=5)
    result = await CHAT_RECAP.handler(ctx, {})
    assert "Пересказ 4 последних сообщений группы (всего в записи 4)" in result
    assert len(link.calls) == 1
    call_args = link.calls[0]
    content = call_args.get("messages", [{}])[0].get("content", "")
    assert "реплика 5" not in content
    assert "реплика 4" in content


async def test_chat_recap_chat_id_arg_ignored(store):
    link = FakeNodeLink("итог")
    await _fill(store, chat_id=-100)
    await _fill(store, chat_id=-999, count=3)
    ctx = _ctx(store, link)
    result = await CHAT_RECAP.handler(ctx, {"chat_id": -999})
    assert "Пересказ 5 последних сообщений группы (всего в записи 5)" in result
    assert len(link.calls) == 1
    call_args = link.calls[0]
    content = call_args.get("messages", [{}])[0].get("content", "")
    assert "реплика 5" in content
    assert ctx.chat_id == call_args["chat_id"] == -100


async def test_chat_recap_count_limit(store):
    link = FakeNodeLink("итог")
    await _fill(store, count=5)
    ctx = _ctx(store, link)
    result = await CHAT_RECAP.handler(ctx, {"count": 2})
    assert "Пересказ 2 последних сообщений группы (всего в записи 5)" in result
    assert len(link.calls) == 1
    call_args = link.calls[0]
    content = call_args.get("messages", [{}])[0].get("content", "")
    assert "реплика 4" in content
    assert "реплика 5" in content
    assert "реплика 3" not in content


async def test_chat_recap_empty_record(store):
    link = FakeNodeLink("итог")
    ctx = _ctx(store, link)
    result = await CHAT_RECAP.handler(ctx, {})
    assert result == EMPTY_RECORD
    assert link.calls == []


@pytest.mark.parametrize(("chat_id", "store_arg"), [(-100, None), (5, "store")])
async def test_chat_recap_no_record_outside_recorded_group(chat_id, store_arg, store):
    link = FakeNodeLink("итог")
    ctx = _ctx(store if store_arg == "store" else None, link, chat_id=chat_id)
    result = await CHAT_RECAP.handler(ctx, {})
    assert result == NO_RECORD


async def test_chat_recap_no_swarm_link(store):
    await _fill(store)
    ctx = _ctx(store, None)
    result = await CHAT_RECAP.handler(ctx, {})
    assert result == NO_SWARM_LINK


async def test_chat_recap_retell_failed(store):
    link = FakeNodeLink("")
    await _fill(store)
    ctx = _ctx(store, link)
    result = await CHAT_RECAP.handler(ctx, {})
    assert result == RETELL_FAILED
    assert ctx.recap_digest is None


async def test_chat_recap_focus_not_matched(store):
    link = FakeNodeLink("итог")
    await _fill(store)
    ctx = _ctx(store, link)
    result = await CHAT_RECAP.handler(ctx, {"focus": "космос"})
    assert "По теме ничего не нашлось" in result


async def test_chat_recap_memory_remember_refusal(store):
    link = FakeNodeLink("итог")
    await _fill(store)
    ctx = _ctx(store, link)
    await CHAT_RECAP.handler(ctx, {})
    sub = Subscription(chat_id=-100, name="g", allowed_commands=frozenset({"*"}))
    ctx.subscription = sub
    args = {"action": memory_protocol.ACTION_REMEMBER, "text": "факт"}
    result = await ai_tools.tool_memory(ctx, args)
    assert result == "не запоминаю: в этом ходе ты читал чужую переписку"
