from __future__ import annotations

import json

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import http, mcp_http
from sa_home_bot.devlab.hub.store import HubStore

PROJECT = "proj"
MODEL = "qwen"
BASE = "base/start"
TOOL_NAMES = {
    protocol.TOOL_MODELS,
    protocol.TOOL_PROJECT_ADD,
    protocol.TOOL_PROJECTS,
    protocol.TOOL_SUBMIT,
    protocol.TOOL_LIST,
    protocol.TOOL_STATUS,
    protocol.TOOL_INBOX,
    protocol.TOOL_ACK,
    protocol.TOOL_ANSWER,
    protocol.TOOL_RESUME,
    protocol.TOOL_STOP,
    protocol.TOOL_RELEASE,
}


@pytest.fixture
async def store(tmp_path):
    hub_store = HubStore(tmp_path)
    await hub_store.open()
    yield hub_store
    await hub_store.close()


@pytest.fixture
async def client(store):
    app = http.build_app(store)
    mcp_http.add_routes(app)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def headers_for(store, name, role=protocol.ROLE_CLIENT):
    return {"Authorization": f"Bearer {await store.issue_token(name, role)}"}


@pytest.fixture
async def alice(store):
    return await headers_for(store, "alice")


@pytest.fixture
async def bob(store):
    return await headers_for(store, "bob")


async def rpc(client, headers, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = await client.post(protocol.HTTP_MCP_PATH, json=body, headers=headers)
    assert resp.status == 200
    return (await resp.json())["result"]


async def call(client, headers, tool, **arguments):
    result = await rpc(client, headers, "tools/call", {"name": tool, "arguments": arguments})
    text = result["content"][0]["text"]
    return result["isError"], json.loads(text) if not result["isError"] else text


async def submit(client, headers, **overrides):
    arguments = {
        "project": PROJECT,
        "model": MODEL,
        "task": "# t",
        "base_branch": BASE,
        **overrides,
    }
    return await call(client, headers, protocol.TOOL_SUBMIT, **arguments)


async def test_real_mcp_client_lists_tools_and_reads_resources(client, alice):
    url = str(client.make_url(protocol.HTTP_MCP_PATH))
    async with (
        httpx2.AsyncClient(headers=alice) as http_client,
        streamable_http_client(url, http_client=http_client) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == TOOL_NAMES
        assert all(tool.description for tool in tools.tools)
        resources = await session.list_resources()
        assert {str(r.uri) for r in resources.resources} == {
            protocol.RESOURCE_TASK_TEMPLATE,
            protocol.RESOURCE_USAGE,
        }
        added = await session.call_tool(protocol.TOOL_PROJECT_ADD, {"name": PROJECT})
        assert not added.is_error
        listed = await session.call_tool(protocol.TOOL_PROJECTS, {})
        assert json.loads(listed.content[0].text) == [PROJECT]


@pytest.mark.parametrize(
    "uri, marker",
    [(protocol.RESOURCE_TASK_TEMPLATE, "## Шаги"), (protocol.RESOURCE_USAGE, "agent/<id>")],
)
async def test_resources_read(client, alice, uri, marker):
    result = await rpc(client, alice, "resources/read", {"uri": uri})
    assert marker in result["contents"][0]["text"]


async def test_protocol_basics(client, alice):
    init = await rpc(client, alice, "initialize", {"protocolVersion": "2025-06-18"})
    assert init["protocolVersion"] == "2025-06-18"
    assert await rpc(client, alice, "ping") == {}
    tools = await rpc(client, alice, "tools/list")
    assert {t["name"] for t in tools["tools"]} == TOOL_NAMES
    note = await client.post(
        protocol.HTTP_MCP_PATH,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=alice,
    )
    assert note.status == 202
    assert (await client.get(protocol.HTTP_MCP_PATH, headers=alice)).status == 405
    bad = await client.post(protocol.HTTP_MCP_PATH, data=b"{", headers=alice)
    assert bad.status == 400


async def test_job_lifecycle(client, store, alice):
    assert not (await call(client, alice, protocol.TOOL_PROJECT_ADD, name=PROJECT))[0]
    _, submitted = await submit(client, alice)
    job_id = submitted["job"]["id"]
    assert submitted["job"]["base_branch"] == BASE
    assert submitted["warnings"]

    _, jobs = await call(client, alice, protocol.TOOL_LIST, statuses=[protocol.STATUS_QUEUED])
    assert [j["id"] for j in jobs] == [job_id]
    assert "task" not in jobs[0]
    _, detail = await call(client, alice, protocol.TOOL_STATUS, id=job_id)
    assert detail["task"] == "# t"

    claimed = await store.queue.claim("pc", [MODEL])
    assert claimed and claimed.id == job_id
    await store.queue.post_from_executor(job_id, protocol.MSG_STARTED, executor="pc")
    await store.queue.post_from_executor(
        job_id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: "как?"}, executor="pc"
    )

    _, first = await call(client, alice, protocol.TOOL_INBOX)
    assert [m["kind"] for m in first["messages"]] == [protocol.MSG_STARTED, protocol.MSG_QUESTION]
    cursor = first["cursor"]
    _, answered = await call(client, alice, protocol.TOOL_ANSWER, id=job_id, text="так")
    assert answered["status"] == protocol.STATUS_RUNNING

    _, acked = await call(client, alice, protocol.TOOL_ACK, upto=cursor)
    assert acked["acked"] == 2
    _, empty = await call(client, alice, protocol.TOOL_INBOX, since=cursor)
    assert empty == {"messages": [], "cursor": cursor}

    _, stopped = await call(client, alice, protocol.TOOL_STOP, id=job_id)
    assert stopped["status"] == protocol.STATUS_STOPPED
    _, resumed = await call(
        client, alice, protocol.TOOL_RESUME, id=job_id, text="правки", model="other"
    )
    assert resumed["status"] == protocol.STATUS_QUEUED
    assert resumed["warnings"]
    assert (await store.queue.get_job(job_id)).model == "other"


async def test_known_model_gives_no_warning(client, store, alice):
    await call(client, alice, protocol.TOOL_PROJECT_ADD, name=PROJECT)
    gateway = await store.issue_token("gw", protocol.ROLE_GATEWAY)
    principal = await store.authenticate(gateway)
    await store.heartbeat(principal, {protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}]})
    _, catalog = await call(client, alice, protocol.TOOL_MODELS)
    assert [m[protocol.MODEL_NAME] for m in catalog] == [MODEL]
    _, submitted = await submit(client, alice)
    assert submitted["warnings"] == []


async def test_release_requires_waiting_owner(client, store, alice):
    await call(client, alice, protocol.TOOL_PROJECT_ADD, name=PROJECT)
    _, submitted = await submit(client, alice)
    is_error, text = await call(client, alice, protocol.TOOL_RELEASE, id=submitted["job"]["id"])
    assert is_error and "release" in text


async def test_other_client_sees_nothing(client, store, alice, bob):
    await call(client, alice, protocol.TOOL_PROJECT_ADD, name=PROJECT)
    _, submitted = await submit(client, alice)
    job_id = submitted["job"]["id"]
    assert (await call(client, bob, protocol.TOOL_LIST))[1] == []
    assert (await call(client, bob, protocol.TOOL_STATUS, id=job_id))[0]
    assert (await call(client, bob, protocol.TOOL_STOP, id=job_id))[0]
    await store.queue.claim("pc", [MODEL])
    await store.queue.post_from_executor(job_id, protocol.MSG_STARTED, executor="pc")
    assert (await call(client, bob, protocol.TOOL_INBOX))[1]["messages"] == []


@pytest.mark.parametrize(
    "tool, arguments",
    [
        (
            protocol.TOOL_SUBMIT,
            {"project": "none", "model": MODEL, "task": "t", "base_branch": BASE},
        ),
        (
            protocol.TOOL_SUBMIT,
            {"project": PROJECT, "model": MODEL, "task": "t", "base_branch": "x"},
        ),
        (protocol.TOOL_SUBMIT, {"project": PROJECT, "model": MODEL}),
        (protocol.TOOL_PROJECT_ADD, {"name": "Плохо Имя"}),
        (protocol.TOOL_STATUS, {"id": "nope00"}),
        (protocol.TOOL_STATUS, {"id": 5}),
        (protocol.TOOL_LIST, {"statuses": ["bogus"]}),
        (protocol.TOOL_ACK, {"upto": True}),
        (protocol.TOOL_STOP, {"id": "a", "extra": 1}),
    ],
)
async def test_hub_error_becomes_tool_error(client, alice, tool, arguments):
    await call(client, alice, protocol.TOOL_PROJECT_ADD, name=PROJECT)
    is_error, text = await call(client, alice, tool, **arguments)
    assert is_error and text


async def test_unknown_tool_is_rpc_error(client, alice):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    }
    resp = await client.post(protocol.HTTP_MCP_PATH, json=body, headers=alice)
    assert (await resp.json())["error"]["code"] == mcp_http.INVALID_PARAMS


@pytest.mark.parametrize(
    "role, status",
    [
        (None, 401),
        (protocol.ROLE_CLIENT, 200),
        (protocol.ROLE_EXECUTOR, 403),
        (protocol.ROLE_GATEWAY, 403),
    ],
)
async def test_roles(client, store, role, status):
    headers = await headers_for(store, "who", role) if role else {}
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    resp = await client.post(protocol.HTTP_MCP_PATH, json=body, headers=headers)
    assert resp.status == status
