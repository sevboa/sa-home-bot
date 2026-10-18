from __future__ import annotations

import json
from functools import partial

from sa_home_bot.bot import tools
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import ProtoError
from tests.unit.tools_support import (
    ADMIN,
    OWN_STATE,
    declared_enum,
    swarm_link,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

_tor_enum = partial(declared_enum, "torrents", "action")


# --- torrents: закачки целиком (список, место, magnet, пауза/запуск) ---


async def test_torrents_list_finds_hosting_node(store):
    """Ноду со службой ищем в состоянии роя, а не хардкодим: назначения
    меняются кнопкой в боте, без правки кода."""
    link = swarm_link(command_result={"torrents": [{"name": "Foo"}], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "list"}
    )
    payload = json.loads(raw)
    assert payload["node"] == "alfred"
    assert payload["count"] == 1
    action, dst = link.commands[0]
    assert (action, dst.node, dst.service) == ("list", "alfred", "torrents")


async def test_torrents_space_passes_through(store):
    link = swarm_link(command_result={"dirs": [{"path": "/mnt/data/pr", "free_bytes": 1}]})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "space"}
    )
    assert json.loads(raw)["dirs"][0]["free_bytes"] == 1
    assert link.commands[0][0] == "space"


async def test_torrents_add_sends_magnet_and_save_path(store):
    link = swarm_link(command_result={"name": "Foo", "save_path": "/mnt/data/pr"})
    await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {
            "action": "add",
            "magnet": "magnet:?xt=urn:btih:abc",
            "save_path": "/mnt/data/pr",
            "name": "Foo",
        },
    )
    assert link.sent_args[0] == {
        "source": "magnet:?xt=urn:btih:abc",
        "save_path": "/mnt/data/pr",
        "name": "Foo",
    }


async def test_torrents_add_takes_a_search_result_url_too(store):
    """Ссылка из выдачи своего же поиска — это не magnet, а адрес метафайла
    на трекере; проверку «а можно ли с этого сайта» делает служба."""
    link = swarm_link(command_result={"name": "Foo", "save_path": "/mnt/data/pr"})
    await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {
            "action": "add",
            "magnet": "https://rutracker.org/forum/dl.php?t=1",
            "save_path": "/mnt/data/pr",
        },
    )
    assert link.sent_args[0]["source"] == "https://rutracker.org/forum/dl.php?t=1"


async def test_torrents_add_refuses_a_source_that_is_not_a_link(store):
    """Base64-файл модели взяться неоткуда — его человек присылает сам."""
    link = swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "add", "magnet": "ZDg6YW5ub3VuY2U="},
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_search_sends_query(store):
    link = swarm_link(command_result={"results": [{"name": "Foo", "seeders": 10}], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "search", "query": "задача трёх тел"},
    )
    assert json.loads(raw)["count"] == 1
    assert link.commands[0][0] == "search"
    assert link.sent_args[0] == {"query": "задача трёх тел"}


async def test_torrents_search_smart_sends_phrase_and_words(store):
    link = swarm_link(command_result={"results": [{"name": "Foo", "seeders": 10}], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "search_smart", "phrase": "задача трёх тел", "words": "2024 1080p"},
    )
    assert json.loads(raw)["count"] == 1
    assert link.commands[0][0] == "search_smart"
    assert link.sent_args[0] == {"phrase": "задача трёх тел", "words": "2024 1080p"}


async def test_torrents_search_smart_without_words_omits_the_key(store):
    link = swarm_link(command_result={"results": [], "count": 0})
    await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "search_smart", "phrase": "задача трёх тел"},
    )
    assert link.sent_args[0] == {"phrase": "задача трёх тел"}


async def test_torrents_search_without_query_asks_for_it(store):
    link = swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "search"}
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_search_smart_without_phrase_asks_for_it(store):
    link = swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "search_smart", "words": "2024"},
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_add_without_save_path_points_at_space(store):
    """Сервер протокола ответил бы «нет обязательного параметра: save_path» —
    формально верно, но не говорит модели, где взять значение."""
    link = swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "add", "magnet": "magnet:?xt=urn:btih:abc"},
    )
    assert result.startswith("ошибка")
    assert "space" in result
    assert link.commands == []


async def test_torrents_pause_sends_name(store):
    link = swarm_link(command_result={"paused": ["Foo.S01"], "count": 1})
    raw = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "pause", "name": "Foo.S01"}
    )
    assert json.loads(raw)["paused"] == ["Foo.S01"]
    assert link.sent_args[0] == {"name": "Foo.S01"}


async def test_torrents_pause_without_name_asks_for_it(store):
    link = swarm_link()
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "pause"}
    )
    assert result.startswith("ошибка")
    assert link.commands == []


async def test_torrents_service_refusal_reads_as_answer(store):
    """Отказ службы (мало места, неоднозначное имя) — готовый ответ модели с
    объяснением, что делать дальше, а не «внутренняя ошибка»."""
    link = swarm_link(command_raises=ProtoError("bad_request", "мало места в /mnt/data/pr"))
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "add", "magnet": "magnet:?xt=urn:btih:abc", "save_path": "/mnt/data/pr"},
    )
    assert result == "не вышло: мало места в /mnt/data/pr"


async def test_torrents_unreachable_service_degrades_to_text(store):
    """§7.3: спящая нода — обычный результат для модели, а не сбой цикла."""
    link = swarm_link(command_raises=ServiceUnavailableError("нода спит"))
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "list"}
    )
    assert result.startswith("недоступно")


async def test_torrents_service_absent_in_swarm(store):
    own = {**OWN_STATE, "services": [{"name": "monitor", "status": "running"}]}
    link = swarm_link(states={"own": own})
    result = await tools.tool_torrents(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "list"}
    )
    assert result.startswith("недоступно")


