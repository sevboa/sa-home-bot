from __future__ import annotations

import asyncio

import pytest

from sa_home_bot.domain.vpn_link import servers_unusable_from_russia
from sa_home_bot.reality.client_config import RealityParams
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.subscription_links import SubscriptionLinksMixin

RUSSIA = ["osip", "vanka"]
IGNORED = ["lab"]


@pytest.mark.parametrize(
    ("net", "links", "expected"),
    [
        (
            [("osip", "ion", "ok"), ("vanka", "ion", "down"), ("osip", "jeeves", "ok")],
            [
                ("osip", "ion", "down"),
                ("osip", "ion", "down"),
                ("vanka", "ion", "ok"),
                ("osip", "jeeves", "ok"),
            ],
            {"ion"},
        ),
        (
            [("vanka", "ion", "down")],
            [("vanka", "ion", "ok")],
            {"ion"},
        ),
        (
            [("osip", "wooster", "ok"), ("vanka", "wooster", "ok")],
            [("osip", "wooster", "partial"), ("vanka", "wooster", "down")],
            set(),
        ),
        (
            [("osip", "ion", "ok")],
            [("osip", "ion", "down"), ("osip", "ion", "ok")],
            set(),
        ),
        (
            [("osip", "ion", "down")],
            [("lab", "ion", "ok")],
            {"ion"},
        ),
        (
            [("osip", "ion", "down"), ("lab", "ion", "ok")],
            [("lab", "ion", "ok")],
            {"ion"},
        ),
        (
            [("lab", "ion", "down")],
            [("lab", "ion", "down")],
            set(),
        ),
        (
            [("tokyo", "ion", "down")],
            [("tokyo", "ion", "down")],
            set(),
        ),
        ([], [], set()),
    ],
)
def test_servers_unusable_from_russia(net, links, expected):
    assert servers_unusable_from_russia(net, links, RUSSIA, IGNORED) == expected


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


@pytest.mark.parametrize(
    ("blocked", "flag", "servers", "expected"),
    [
        ({"ion"}, True, ["jeeves", "ion"], ["jeeves"]),
        (set(), True, ["jeeves", "ion"], ["jeeves", "ion"]),
        ({"jeeves", "ion"}, True, ["jeeves", "ion"], ["jeeves", "ion"]),
        ({"ion"}, False, ["jeeves", "ion"], ["jeeves", "ion"]),
    ],
)
def test_hide_blocked_from_russia(blocked, flag, servers, expected):
    class Fake(SubscriptionLinksMixin):
        _cfg = type("obj", (), {"sub_hide_blocked_from_russia": flag})()

        async def _servers_unusable_from_russia(self):
            return blocked

    entries, awg_entries, nodes = asyncio.run(
        Fake()._sub_hide_blocked(
            [_entry(s) for s in servers],
            [_awg(s) for s in servers],
            [_info(s) for s in servers],
        )
    )
    assert [e.node for e in entries] == expected
    assert [e.node for e in awg_entries] == expected
    assert [n.node for n in nodes] == expected
