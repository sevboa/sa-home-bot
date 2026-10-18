from __future__ import annotations

import pytest

from sa_home_bot.bot import tools
from sa_home_bot.bot.tools import DismissalBox
from tests.unit.tools_support import (
    ADMIN,
    declared_enum,
    make_ctx,
    make_sub,
    swarm_link,
    tool_names,
)

ALL_TORRENT_ACTIONS = [
    "list",
    "space",
    "search",
    "search_smart",
    "details",
    "add",
    "pause",
    "resume",
]
ALL_VPN_ACTIONS = [
    "usage", "issue", "settings", "report", "grant_extra", "request_extra", "apk", "peers",
    "resolve_request", "proxy_link", "proxy_rotate_secret", "proxy_usage",
]  # fmt: skip


def test_tools_for_admin_gets_everything():
    assert set(tool_names(ADMIN)) == {spec.name for spec in tools.TOOLS}


def test_tools_for_without_subscription_is_fail_closed():
    toolkit = tools.tools_for(None)
    unrestricted = {s.name for s in tools.TOOLS if s.requires is None and s.variants is None}
    assert {d["function"]["name"] for d in toolkit.declarations} == unrestricted
    assert set(toolkit.handlers) == unrestricted


@pytest.mark.parametrize(
    ("tool", "param", "rights", "expected"),
    [
        ("swarm_status", "what", ("nodes",), ["nodes"]),
        ("swarm_status", "what", ("*",), ["nodes", "health", "disks"]),
        ("node_manage", "action", ("check_update@node",), ["check_update", "check_all"]),
        (
            "node_manage",
            "action",
            ("check_update@node", "update@node"),
            ["check_update", "check_all", "update"],
        ),
        ("node_manage", "action", ("*",), ["check_update", "check_all", "update", "restart_node"]),
        ("torrents", "action", ("list@torrents",), ["list"]),
        ("torrents", "action", ("list@torrents", "pause@torrents"), ["list", "pause"]),
        ("torrents", "action", ("search@torrents",), ["search", "search_smart"]),
        ("torrents", "action", ("*@torrents",), ALL_TORRENT_ACTIONS),
        ("torrents", "action", ("*",), ALL_TORRENT_ACTIONS),
        ("dismiss", "mode", ("sleep@llm",), ["model"]),
        ("dismiss", "mode", ("suspend@node",), ["sleep"]),
        ("dismiss", "mode", ("*",), ["model", "sleep", "off"]),
        ("vpn", "action", ("usage@vpn",), ["usage", "report"]),
        ("vpn", "action", ("*",), ALL_VPN_ACTIONS),
        ("memory", "action", ("recall@memory",), ["recall"]),
    ],
)
def test_variant_enum_is_narrowed_to_granted_rights(tool, param, rights, expected):
    assert declared_enum(tool, param, make_sub(*rights)) == expected


@pytest.mark.parametrize(
    ("tool", "rights"),
    [
        ("swarm_status", ("ai",)),
        ("node_manage", ("ai",)),
        ("swarm_events", ("ai",)),
        ("torrents", ("status", "nodes")),
        ("dismiss", ("chat@llm", "status")),
        ("vpn", ("status", "nodes")),
        ("memory", ("status", "nodes")),
        ("web_search", ("status", "nodes")),
    ],
)
def test_tool_is_hidden_without_any_matching_right(tool, rights):
    subscription = make_sub(*rights)
    assert tool not in tool_names(subscription)
    assert tool not in tools.tools_for(subscription).handlers


def test_web_search_appears_with_search_right():
    assert "web_search" in tool_names(make_sub("search@net"))


def test_recall_tool_result_needs_no_rights():
    assert "recall_tool_result" in tool_names(make_sub())


def test_filtering_does_not_mutate_shared_declaration():
    declared_enum("swarm_status", "what", make_sub("nodes"))
    assert declared_enum("swarm_status", "what", ADMIN) == ["nodes", "health", "disks"]


VARIANT_TOOL_CASES = [
    pytest.param(tools.tool_swarm_status, ("nodes",), {"what": "disks"}, id="swarm_status"),
    pytest.param(
        tools.tool_node_manage, ("check_update@node",), {"action": "restart_node"}, id="node_manage"
    ),
    pytest.param(
        tools.tool_torrents, ("list@torrents",), {"action": "pause", "name": "все"}, id="torrents"
    ),
    pytest.param(tools.tool_dismiss, ("sleep@llm",), {"mode": "off"}, id="dismiss"),
    pytest.param(tools.tool_memory, ("recall@memory",), {"action": "forget", "id": 1}, id="memory"),
    pytest.param(tools.tool_vpn, ("usage@vpn",), {"action": "issue"}, id="vpn"),
]


@pytest.mark.parametrize(("handler", "rights", "args"), VARIANT_TOOL_CASES)
async def test_handler_rechecks_rights_for_a_value_missing_from_enum(handler, rights, args):
    link, box = swarm_link(), DismissalBox()
    ctx = make_ctx(node_link=link, subscription=make_sub(*rights), dismissal=box)
    assert (await handler(ctx, args)).startswith("не умею")
    assert link.commands == []
    assert box.mode is None


@pytest.mark.parametrize(("handler", "rights", "args"), VARIANT_TOOL_CASES)
async def test_handler_without_subscription_refuses(handler, rights, args):
    box = DismissalBox()
    ctx = make_ctx(node_link=swarm_link(), subscription=None, dismissal=box)
    assert (await handler(ctx, args)).startswith("не умею")
    assert box.mode is None
