from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import partial

from sa_home_bot.bot import tools
from tests.unit.tools_support import (
    ADMIN,
    OWN_STATE,
    FakeSwarmLink,
    declared_enum,
    swarm_link,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

swarm_enum = partial(declared_enum, "swarm_status", "what")


# --- swarm_status: сам обработчик (сбор данных через wake_core) ---


async def test_swarm_status_nodes_marks_sleeping_workstation_as_normal(store):
    link = swarm_link()
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes"}
    )
    nodes = {n["node"]: n for n in json.loads(raw)["nodes"]}
    assert nodes["alfred"]["online"] is True
    assert nodes["alfred"]["services_running"] == 2
    # winpc — рабочая станция: выключена = норма, а не авария (ARCH §11 п. 4).
    assert nodes["winpc"]["online"] is False
    assert nodes["winpc"]["sleeping_is_normal"] is True


async def test_swarm_status_offline_always_on_node_is_not_normal(store):
    own = {**OWN_STATE, "peers": [{"id": "jeeves", "alive": False, "kind": "vps"}]}
    link = swarm_link(states={"own": own})
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes"}
    )
    jeeves = next(n for n in json.loads(raw)["nodes"] if n["node"] == "jeeves")
    assert jeeves["sleeping_is_normal"] is False


async def test_swarm_status_health_returns_temperatures(store):
    link = swarm_link()
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "health", "node": "alfred"}
    )
    entry = json.loads(raw)["health"][0]
    assert entry["node"] == "alfred"
    assert entry["components"][0]["temperature_c"] == 37.0


async def test_swarm_status_disks_returns_free_space(store):
    link = swarm_link()
    raw = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "disks", "node": "alfred"}
    )
    disk = json.loads(raw)["disks"][0]["disks"][0]
    assert disk["label"] == "eMMC"
    assert disk["free_bytes"] == 51000000000


async def test_swarm_status_unknown_node_lists_known_ones(store):
    link = swarm_link()
    result = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes", "node": "нету"}
    )
    assert "нет такой ноды" in result
    assert "alfred" in result and "winpc" in result


async def test_swarm_status_own_node_down(store):
    link = FakeSwarmLink(states={})
    result = await tools.tool_swarm_status(
        _ctx(store, node_link=link, subscription=ADMIN), {"what": "nodes"}
    )
    assert result.startswith("недоступно")


# --- swarm_events: журнал того, что уже случилось (для Альфреда) ---


async def test_swarm_events_lists_recent(store):
    await store.record_event("node_down", "mycraft", "пропала", datetime.now(tz=UTC))
    result = await tools.tool_swarm_events(
        _ctx(store, subscription=ADMIN), {}
    )
    assert "пропала" in result and "node_down" in result


async def test_swarm_events_filters_by_node(store):
    now = datetime.now(tz=UTC)
    await store.record_event("node_down", "mycraft", "mycraft пропала", now)
    await store.record_event("node_down", "jeeves", "jeeves пропала", now)
    result = await tools.tool_swarm_events(
        _ctx(store, subscription=ADMIN), {"node": "jeeves"}
    )
    assert "jeeves пропала" in result
    assert "mycraft пропала" not in result


async def test_swarm_events_filters_by_hours(store):
    now = datetime.now(tz=UTC)
    await store.record_event("node_down", "mycraft", "старое", now - timedelta(hours=5))
    await store.record_event("node_up", "mycraft", "свежее", now)
    result = await tools.tool_swarm_events(_ctx(store, subscription=ADMIN), {"hours": 1})
    assert "свежее" in result
    assert "старое" not in result


async def test_swarm_events_empty_says_so(store):
    result = await tools.tool_swarm_events(_ctx(store, subscription=ADMIN), {})
    assert "не нашлось" in result


async def test_swarm_events_rejects_bad_hours(store):
    result = await tools.tool_swarm_events(
        _ctx(store, subscription=ADMIN), {"hours": "вчера"}
    )
    assert "должен быть числом" in result


async def test_swarm_events_no_store_degrades_to_text(store):
    # Служба tasks (второй пользователь этого модуля) не имеет доступа к БД
    # бота — тот же приём деградации, что и у tool_tell без book/notifier.
    result = await tools.tool_swarm_events(_ctx(None, subscription=ADMIN), {})
    assert result.startswith("недоступно")
