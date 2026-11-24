from __future__ import annotations

import asyncio

import pytest

from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.subscription_links import SubscriptionLinksMixin


def _entry(node: str) -> subs.SubEntry:
    return subs.SubEntry(
        node=node,
        name=node,
        uuid="u",
        params=RealityParams("198.51.100.1", 8443, "PUB", "ab", "www.google.com"),
    )


def _awg(node: str) -> subs.AwgSubEntry:
    return subs.AwgSubEntry(
        node=node,
        name=node,
        version="2",
        private_key="k",
        address="10.0.0.2",
        server_public_key="p",
        endpoint_host="198.51.100.1",
        port=51820,
        dns="1.1.1.1",
        mtu=1280,
        obfuscation=(),
    )


def _info(node: str) -> subs.NodeInfo:
    return subs.NodeInfo(node=node, name=node, ip="198.51.100.1")


def _run(russia: dict[str, str], flag: bool, servers: list[str]):
    class Fake(SubscriptionLinksMixin):
        _cfg = type("obj", (), {"sub_hide_blocked_from_russia": flag})()

        async def _net_scope_levels(self):
            return {"global": {}, "russia": russia}

    return asyncio.run(
        Fake()._sub_hide_blocked(
            [_entry(s) for s in servers],
            [_awg(s) for s in servers],
            [_info(s) for s in servers],
        )
    )


@pytest.mark.parametrize(
    ("russia", "flag", "servers", "expected"),
    [
        ({"ion": "down"}, True, ["jeeves", "ion"], ["jeeves"]),
        ({"ion": "partial"}, True, ["jeeves", "ion"], ["jeeves", "ion"]),
        ({"ion": "ok"}, True, ["jeeves", "ion"], ["jeeves", "ion"]),
        ({}, True, ["jeeves", "ion"], ["jeeves", "ion"]),
        ({"jeeves": "down", "ion": "down"}, True, ["jeeves", "ion"], ["jeeves", "ion"]),
        ({"ion": "down"}, False, ["jeeves", "ion"], ["jeeves", "ion"]),
    ],
)
def test_hide_blocked_from_russia(russia, flag, servers, expected):
    entries, awg_entries, nodes = _run(russia, flag, servers)
    assert [e.node for e in entries] == expected
    assert [e.node for e in awg_entries] == expected
    assert [n.node for n in nodes] == expected
