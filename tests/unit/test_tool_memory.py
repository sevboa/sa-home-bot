from __future__ import annotations

import json

from sa_home_bot.bot import tools
from sa_home_bot.proto.errors import ServiceUnavailableError
from tests.unit.tools_support import (
    ADMIN,
    FakeSwarmLink,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

# --- memory: долгая память о чате ---


async def test_memory_tool_never_lets_the_model_choose_whose_memory(store):
    """chat_id проставляет бот из контекста, а не модель: иначе «вспомни, что
    тебе говорили в другом чате» стало бы рабочей просьбой."""
    link = FakeSwarmLink(command_result={"facts": [{"id": 1, "text": "Качаем в /mnt/data/pr"}]})
    await tools.tool_memory(
        _ctx(store, node_link=link, subscription=ADMIN, chat_id=777),
        {"action": "recall", "query": "куда качаем", "chat_id": 999},
    )
    action, dst = link.commands[0]
    assert (action, dst.node, dst.service) == ("recall", "mycraft", "memory")
    assert link.sent_args[0]["chat_id"] == 777  # свой чат, не подсунутый моделью


async def test_memory_tool_does_not_expose_scope_to_the_model():
    """Общее знание дома заводит человек руками — сказанное в одном чате не
    должно вдруг стать видимым во всех."""
    decl = next(
        d for d in tools.tools_for(ADMIN).declarations if d["function"]["name"] == "memory"
    )
    params = decl["function"]["parameters"]["properties"]
    assert "scope" not in params and "chat_id" not in params


async def test_memory_recall_without_facts_reads_as_plain_text(store):
    link = FakeSwarmLink(command_result={"facts": [], "count": 0})
    result = await tools.tool_memory(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "recall", "query": "что-то"}
    )
    assert result == "в памяти про это ничего нет"


async def test_memory_outside_a_dialogue_is_honest(store):
    result = await tools.tool_memory(
        _ctx(store, node_link=FakeSwarmLink(), subscription=ADMIN, chat_id=None),
        {"action": "recall", "query": "что-то"},
    )
    assert result.startswith("недоступно")


async def test_remember_piggybacks_a_graph_episode(store):
    """Этап 41: успешный remember дублирует факт в graph_memory (mycraft)
    как ACTION_ADD_EPISODE — best-effort ПОВЕРХ основной записи."""
    link = FakeSwarmLink(
        command_result={
            "id": 1,
            "text": "Наташа — жена Алексея",
            "scope": "chat",
            "sensitive": False,
        }
    )
    result = await tools.tool_memory(
        _ctx(store, node_link=link, subscription=ADMIN, chat_id=777),
        {"action": "remember", "text": "Наташа — жена Алексея"},
    )
    assert json.loads(result)["id"] == 1
    assert len(link.commands) == 2
    remember_action, remember_dst = link.commands[0]
    assert (remember_action, remember_dst.node, remember_dst.service) == (
        "remember",
        "mycraft",
        "memory",
    )
    episode_action, episode_dst = link.commands[1]
    assert (episode_action, episode_dst.node, episode_dst.service) == (
        "add_episode",
        "mycraft",
        "graph_memory",
    )
    assert link.sent_args[1]["text"] == "Наташа — жена Алексея"
    assert link.sent_args[1]["chat_id"] == 777


async def test_remember_survives_graph_memory_being_asleep(store):
    """mycraft штатно спит (ОСЛАБЛЕННЫЙ инвариант, в отличие от memory) —
    сбой piggyback не должен портить уже успешный remember."""

    class FlakyLink(FakeSwarmLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            if action == "add_episode":
                raise ServiceUnavailableError("mycraft спит")
            return await super().command(action, args=args, dst=dst, timeout=timeout)

    link = FlakyLink(command_result={"id": 1, "text": "факт", "scope": "chat", "sensitive": False})
    result = await tools.tool_memory(
        _ctx(store, node_link=link, subscription=ADMIN, chat_id=777),
        {"action": "remember", "text": "факт"},
    )
    assert json.loads(result)["id"] == 1
