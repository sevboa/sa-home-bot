from __future__ import annotations

from functools import partial

from sa_home_bot.bot import tools
from sa_home_bot.proto.messages import ProtoError
from sa_home_bot.vpn import protocol as vpn_protocol
from tests.unit.tools_support import (
    ADMIN,
    FakeSwarmLink,
    declared_enum,
)
from tests.unit.tools_support import (
    make_ctx as _ctx,
)
from tests.unit.tools_support import (
    make_sub as _sub,
)

_vpn_enum = partial(declared_enum, "vpn", "action")


_VPN_OWN_STATE = {
    "node": "jeeves",
    "kind": "vps",
    "peers": [],
    "services": [{"name": "vpn", "service": "vpn", "status": "running"}],
}


def _vpn_link(**kwargs):
    states = {"own": _VPN_OWN_STATE}
    states.update(kwargs.pop("states", {}))
    return FakeSwarmLink(states=states, **kwargs)


async def test_vpn_ceiling_error_tells_model_to_request_extra(store):
    error = ProtoError(vpn_protocol.ERR_QUOTA_CEILING, "потолок")
    link = _vpn_link(command_raises=error)
    result = await tools.tool_vpn(
        _ctx(store, node_link=link, subscription=ADMIN), {"action": "grant_extra"}
    )
    assert "request_extra" in result


async def test_vpn_usage_all_guests_requires_admin_right(store):
    link = _vpn_link(command_result={"chats": [], "node": {"free_bytes": 1}})
    guest = _sub("usage@vpn")
    result = await tools.tool_vpn(
        _ctx(store, node_link=link, subscription=guest, chat_id=777),
        {"action": "usage", "all_guests": True},
    )
    assert result.startswith("недоступно")
    assert link.commands == []


async def test_vpn_usage_all_guests_admin_gets_node_wide_summary(store):
    link = _vpn_link(command_result={"chats": [{"chat_id": 777}], "node": {"free_bytes": 1}})
    result = await tools.tool_vpn(
        _ctx(store, node_link=link, subscription=ADMIN, chat_id=1),
        {"action": "usage", "all_guests": True},
    )
    assert "free_bytes" in result
    action, dst = link.commands[0]
    assert (action, dst.node, dst.service) == ("usage", "jeeves", "vpn")
    assert link.sent_args[0] == {}
