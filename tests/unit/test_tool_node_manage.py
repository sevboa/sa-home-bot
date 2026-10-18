from __future__ import annotations

import json
from functools import partial

from sa_home_bot.bot import tools
from sa_home_bot.proto.errors import ServiceUnavailableError
from sa_home_bot.proto.messages import Address, ProtoError
from sa_home_bot.tasks import protocol as task_protocol
from tests.unit.tools_support import (
    ADMIN,
    FakeSwarmLink,
    declared_enum,
    swarm_link,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)

_node_enum = partial(declared_enum, "node_manage", "action")


async def test_node_manage_check_update_own_node(store):
    link = swarm_link(
        command_result={
            "repo": "git@example.com:sa-home-bot",
            "running": "0.69.0",
            "installed": "0.69.0",
            "latest": "0.69.0",
        }
    )
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "check_update"}
    )
    assert link.commands == [("check_update", None)]
    assert json.loads(result)["latest"] == "0.69.0"


async def test_node_manage_update_reports_target_version(store):
    link = swarm_link(command_result={"scheduled": True, "target_version": "0.70.0"})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "update"}
    )
    assert "0.70.0" in result
    assert "restart_node" in result


async def test_node_manage_update_scheduled_auto_schedules_wake_and_warns(store):
    link = swarm_link(command_result={"scheduled": True, "target_version": "0.70.0"})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "update", "node": "winpc"}
    )
    assert "НЕ вызывай restart_node прямо сейчас" in result
    assert "winpc" in result
    assert "поставлено автоматически" in result
    create_calls = [
        (action, args)
        for action, args in zip(link.commands, link.sent_args, strict=True)
        if action[0] == task_protocol.ACTION_CREATE
    ]
    assert len(create_calls) == 1
    _action, create_args = create_calls[0]
    assert create_args["await_event"] == {
        "node": "winpc",
        "event_type": tools.EVENT_UPDATE_FINISHED,
    }


async def test_node_manage_update_auto_schedule_snapshot_has_real_result_not_placeholder(store):
    history = [
        {"role": "user", "content": "обнови ноды"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "function": {
                        "name": "node_manage",
                        "arguments": {"action": "update", "node": "winpc"},
                    }
                }
            ],
        },
    ]
    link = swarm_link(command_result={"scheduled": True, "target_version": "0.70.0"})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN, history=history),
        {"action": "update", "node": "winpc"},
    )

    create_calls = [
        args
        for action, args in zip(link.commands, link.sent_args, strict=True)
        if action[0] == task_protocol.ACTION_CREATE
    ]
    assert len(create_calls) == 1
    snapshot_messages = create_calls[0]["args"]["messages"]
    own_tool_result = snapshot_messages[-2]
    assert own_tool_result["role"] == "tool"
    assert own_tool_result["content"] != "(результат не сохранён)"
    assert own_tool_result["content"] in result


async def test_node_manage_update_scheduled_on_own_node_resolves_its_id(store):
    link = swarm_link(command_result={"scheduled": True, "target_version": "0.70.0"})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "update"}
    )
    assert "поставлено автоматически" in result
    create_calls = [
        args
        for action, args in zip(link.commands, link.sent_args, strict=True)
        if action[0] == task_protocol.ACTION_CREATE
    ]
    assert len(create_calls) == 1
    assert create_calls[0]["await_event"] == {
        "node": "alfred",
        "event_type": tools.EVENT_UPDATE_FINISHED,
    }


async def test_node_manage_update_up_to_date(store):
    link = swarm_link(command_result={"up_to_date": True, "version": "0.69.0"})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "update"}
    )
    assert "Уже последняя версия" in result


async def test_node_manage_restart_own_node_warns_about_disappearing(store):
    link = swarm_link(command_result={"scheduled": "restart_node", "delay_s": 5})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "restart_node"}
    )
    assert link.commands == [("restart_node", None)]
    assert "ненадолго пропаду" in result


async def test_node_manage_restart_remote_node_targets_dst(store):
    link = swarm_link(command_result={"scheduled": "restart_node", "delay_s": 5})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "restart_node", "node": "winpc"},
    )
    assert link.commands[0] == ("restart_node", Address(node="winpc", service="node"))
    assert "ненадолго пропаду" not in result
    assert "winpc" in result
    assert "поставлено автоматически" in result
    create_calls = [
        args
        for action, args in zip(link.commands, link.sent_args, strict=True)
        if action[0] == task_protocol.ACTION_CREATE
    ]
    assert len(create_calls) == 1
    assert create_calls[0]["await_event"] == {
        "node": "winpc",
        "event_type": tools.EVENT_RESTART_APPLIED,
    }
    directive = create_calls[0]["args"]["messages"][-1]["content"]
    assert "следующая нода" not in directive
    assert "переходить не нужно" in directive


async def test_node_manage_unknown_node_lists_known_ones(store):
    link = swarm_link()
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "check_update", "node": "нету"},
    )
    assert "нет такой ноды" in result
    assert "alfred" in result and "winpc" in result
    assert link.commands == []


async def test_node_manage_service_unavailable_degrades_to_text(store):
    link = swarm_link(command_raises=ServiceUnavailableError("нода спит"))
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "restart_node"}
    )
    assert result.startswith("недоступно")


async def test_node_manage_proto_error_reads_as_answer(store):
    link = swarm_link(command_raises=ProtoError("unknown_action", "нет такого действия"))
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "update"}
    )
    assert result == "не вышло: нет такого действия"


async def test_node_manage_own_node_down(store):
    link = FakeSwarmLink(states={})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN),
        {"action": "check_update", "node": "winpc"},
    )
    assert result.startswith("недоступно")


async def test_node_manage_check_all_summarizes_fleet(store):
    own = {
        "node": "alfred",
        "kind": "server",
        "version": "0.70.1",
        "peers": [
            {"id": "mycraft", "alive": True, "kind": "workstation"},
            {"id": "jeeves", "alive": True, "kind": "vps"},
            {"id": "winpc", "alive": False, "kind": "workstation"},
        ],
    }
    link = swarm_link(
        states={
            "own": own,
            "mycraft:node": {"node": "mycraft", "version": "0.70.0"},
            "jeeves:node": {
                "node": "jeeves",
                "version": "0.70.1",
                "update": {"restart_required": True},
            },
        },
        command_result={
            "repo": "git@example.com:sa-home-bot",
            "running": "0.70.1",
            "installed": "0.70.1",
            "latest": "0.70.1",
        },
    )
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "check_all"}
    )
    assert "v0.70.1" in result
    assert "Нужен update" in result and "mycraft" in result
    assert "restart_node" in result and "jeeves" in result
    assert "Не отвечают" in result and "winpc" in result


async def test_node_manage_check_all_reports_up_to_date(store):
    own = {
        "node": "alfred",
        "kind": "server",
        "version": "0.70.1",
        "peers": [{"id": "mycraft", "alive": True, "kind": "workstation"}],
    }
    link = swarm_link(
        states={"own": own, "mycraft:node": {"node": "mycraft", "version": "0.70.1"}},
        command_result={
            "repo": "git@example.com:sa-home-bot",
            "running": "0.70.1",
            "installed": "0.70.1",
            "latest": "0.70.1",
        },
    )
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "check_all"}
    )
    assert "все доступные ноды на последней версии" in result.lower()


async def test_node_manage_check_all_own_node_down(store):
    link = FakeSwarmLink(states={})
    result = await tools.tool_node_manage(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "check_all"}
    )
    assert result.startswith("недоступно")
