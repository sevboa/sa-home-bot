from __future__ import annotations

import json

from sa_home_bot.bot import tools
from sa_home_bot.proto.errors import ServiceUnavailableError
from tests.unit.tools_support import (
    ADMIN,
    CHAT_ID,
    FakeSwarmLink,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

# --- web_search: интернет через службу net ---


async def test_web_search_calls_net_service(store):
    link = FakeSwarmLink(
        command_result={"query": "погода", "results": [{"title": "T", "url": "u"}], "count": 1}
    )
    raw = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "погода"}
    )
    assert json.loads(raw)["count"] == 1
    action, dst = link.commands[0]
    assert (action, dst.node, dst.service) == ("search", "mycraft", "net")


async def test_web_search_waits_longer_than_net_itself(store):
    """Живая находка 2026-09-25: без явного таймаута действовали 10 с
    ProtoClient по умолчанию, а net ждёт SearXNG 20 с — медленный движок
    обрывал поиск у бота раньше, чем у самой службы."""
    link = FakeSwarmLink(command_result={"query": "q", "results": [], "count": 0})
    ctx = _ctx(store, node_link=link, subscription=ADMIN)
    await tools.tool_web_search(ctx, {"query": "q"})
    assert link.timeouts[0] > ctx.settings.net.request_timeout_s


async def test_web_search_empty_results_reads_as_plain_text(store):
    """Пустая выдача — не ошибка и не JSON: модели проще не пересказывать
    пустой список, а прямо сказать, что ничего не нашлось."""
    link = FakeSwarmLink(command_result={"query": "чепуха", "results": [], "count": 0})
    result = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "чепуха"}
    )
    assert "ничего не нашлось" in result


async def test_web_search_unavailable_degrades_to_text(store):
    link = FakeSwarmLink(command_raises=ServiceUnavailableError("net спит"))
    result = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "q"}
    )
    assert result.startswith("недоступно")


async def test_web_search_without_query(store):
    result = await tools.tool_web_search(
        _ctx(store, node_link=FakeSwarmLink(), subscription=ADMIN), {}
    )
    assert result.startswith("ошибка")


async def test_web_search_piggybacks_a_graph_episode(store):
    """Этап 42.3: успешный поиск дублируется в graph_memory как
    ACTION_ADD_EPISODE с source=web_search — тем же best-effort приёмом,
    что и remember/look_at_photo (тесты выше в модуле)."""
    link = FakeSwarmLink(
        command_result={
            "query": "погода",
            "results": [{"title": "Погода в городе", "url": "u", "snippet": "ясно, +20"}],
            "count": 1,
        }
    )
    await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "погода"}
    )

    assert len(link.commands) == 2
    episode_action, episode_dst = link.commands[1]
    assert episode_action == "add_episode"
    assert episode_dst.node == "mycraft"
    assert episode_dst.service == "graph_memory"
    episode_args = link.sent_args[1]
    assert episode_args["chat_id"] == CHAT_ID
    assert episode_args["source"] == "web_search"
    assert "Погода в городе" in episode_args["text"]
    assert "ясно, +20" in episode_args["text"]


async def test_web_search_empty_results_skips_piggyback(store):
    link = FakeSwarmLink(command_result={"query": "чепуха", "results": [], "count": 0})
    await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "чепуха"}
    )
    assert len(link.commands) == 1


async def test_web_search_survives_graph_memory_being_asleep(store):
    """Этап 42.3: сбой piggyback не должен портить уже успешный ответ."""

    class FlakyLink(FakeSwarmLink):
        async def command(self, action, args=None, dst=None, *, timeout=None):
            if action == "add_episode":
                raise ServiceUnavailableError("mycraft спит")
            return await super().command(action, args=args, dst=dst, timeout=timeout)

    link = FlakyLink(
        command_result={"query": "погода", "results": [{"title": "T"}], "count": 1}
    )
    result = await tools.tool_web_search(
        _ctx(store, node_link=link, subscription=ADMIN), {"query": "погода"}
    )
    assert json.loads(result)["count"] == 1
