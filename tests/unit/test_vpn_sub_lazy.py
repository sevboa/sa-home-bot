from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from sa_home_bot.vpn import protocol as vpn_protocol
from sa_home_bot.vpn import subscription as subs
from sa_home_bot.vpn.protocol import PROBE_CHAT_ID, TRANSPORT_AWG, TRANSPORT_REALITY

from .test_vpn_service import CHAT
from .test_vpn_subscription import _make

LABEL = "📱 iPhone"


@pytest_asyncio.fixture
async def node(tmp_path):
    svc, db = await _make(
        tmp_path, "ion", "198.51.100.3", "🇩🇪 Германия", [TRANSPORT_REALITY, TRANSPORT_AWG]
    )
    yield svc
    await db.close()


async def _count(svc, chat_id=CHAT):
    cur = await svc._db.conn.execute(
        "SELECT COUNT(*) AS n FROM vpn_peers WHERE chat_id = ? AND device_label = ? "
        "AND transport = ? AND status = 'active' AND purpose = 'app'",
        (chat_id, LABEL, TRANSPORT_REALITY),
    )
    return (await cur.fetchone())["n"]


def _args(svc, chat_id=CHAT, label=LABEL, gen=0, token_chat=None):
    token = subs.make_token(svc._sub_secret, token_chat or chat_id, label, gen)
    return {"token": token, "chat_id": chat_id, "device_label": label}


async def test_hint_issues_one_reality_peer_with_awg(node):
    reply = await node._sub_links(_args(node))
    assert reply["found"] and reply["device_label"] == LABEL and reply["chat_id"] == CHAT
    assert reply["entry"]["uuid"]
    assert reply["awg_entries"]
    assert await _count(node) == 1
    again = await node._sub_links(_args(node))
    assert again["found"] and again["entry"]["uuid"] == reply["entry"]["uuid"]
    assert await _count(node) == 1


async def test_parallel_requests_issue_one_peer(node):
    args = _args(node)
    replies = await asyncio.gather(*(node._sub_links(args) for _ in range(5)))
    assert all(r["found"] for r in replies)
    assert len({r["entry"]["uuid"] for r in replies}) == 1
    assert await _count(node) == 1


@pytest.mark.parametrize(
    "case",
    ["bad_hmac", "no_access", "probe", "zero_chat", "empty_label", "no_reality", "stale_gen"],
)
async def test_no_peer_without_valid_hint(node, case):
    args = _args(node)
    if case == "bad_hmac":
        args = _args(node, token_chat=CHAT + 5)
    elif case == "no_access":
        args = _args(node, chat_id=CHAT + 1)
    elif case == "probe":
        args = _args(node, chat_id=PROBE_CHAT_ID)
    elif case == "zero_chat":
        args = _args(node, chat_id=0)
    elif case == "empty_label":
        args = _args(node, label="")
    elif case == "no_reality":
        node._reality = None
    elif case == "stale_gen":
        await node.run_command(
            vpn_protocol.ACTION_ISSUE,
            {"chat_id": CHAT, "device_label": LABEL, "transport": TRANSPORT_REALITY},
        )
        await node.run_command(
            vpn_protocol.ACTION_REVOKE,
            {"chat_id": CHAT, "device_label": LABEL, "transport": TRANSPORT_REALITY},
        )
    reply = await node._sub_links(args)
    assert not reply["found"]
    assert await _count(node, args["chat_id"]) == 0


async def test_issue_failure_keeps_old_reply(node, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("xray")

    monkeypatch.setattr(node, "_issue_locked", boom)
    reply = await node._sub_links(_args(node))
    assert not reply["found"] and reply["info"]["device"]
    assert await _count(node) == 0
