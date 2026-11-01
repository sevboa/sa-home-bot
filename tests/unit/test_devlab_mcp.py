from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from aiohttp.test_utils import TestClient, TestServer
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import http, mcp_http
from sa_home_bot.devlab.hub.store import HubStore
from sa_home_bot.utils.process import run
from tests.unit.support.fakes import FakeNodes

PROJECT = "proj"
MODEL = "qwen"
BASE = "base/start"
TOOL_NAMES = {
    protocol.TOOL_MODELS,
    protocol.TOOL_PROJECT_ADD,
    protocol.TOOL_PROJECTS,
    protocol.TOOL_SUBMIT,
    protocol.TOOL_TEST,
    protocol.TOOL_TEST_LOG,
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
    app = http.build_app(store, FakeNodes())
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


async def register(client, store, headers, branch=BASE):
    is_error, _ = await call(client, headers, protocol.TOOL_PROJECT_ADD, name=PROJECT)
    assert not is_error
    repo = str(store.git_root / f"{PROJECT}{protocol.GIT_REPO_SUFFIX}")
    tree = await run(["git", "--git-dir", repo, "hash-object", "-t", "tree", "/dev/null"])
    ident = ["-c", "user.name=t", "-c", "user.email=t@t"]
    commit = await run(
        ["git", *ident, "--git-dir", repo, "commit-tree", tree.stdout_text.strip(), "-m", "b"]
    )
    ref = protocol.HEADS_REF_PREFIX + branch
    await run(["git", "--git-dir", repo, "update-ref", ref, commit.stdout_text.strip()])


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
    await register(client, store, alice)
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
    await register(client, store, alice)
    gateway = await store.issue_token("gw", protocol.ROLE_GATEWAY)
    principal = await store.authenticate(gateway)
    await store.heartbeat(principal, {protocol.FIELD_MODELS: [{protocol.MODEL_NAME: MODEL}]})
    _, catalog = await call(client, alice, protocol.TOOL_MODELS)
    assert [m[protocol.MODEL_NAME] for m in catalog] == [MODEL]
    _, submitted = await submit(client, alice)
    assert submitted["warnings"] == []


async def test_submit_without_base_branch_pushed(client, store, alice):
    await register(client, store, alice)
    is_error, text = await submit(client, alice, base_branch="base/missing")
    assert is_error and "git push" in text and "base/missing" in text
    assert await store.queue.list_jobs() == []


async def test_release_requires_waiting_owner(client, store, alice):
    await register(client, store, alice)
    _, submitted = await submit(client, alice)
    is_error, text = await call(client, alice, protocol.TOOL_RELEASE, id=submitted["job"]["id"])
    assert is_error and "release" in text


async def test_other_client_sees_nothing(client, store, alice, bob):
    await register(client, store, alice)
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
async def test_hub_error_becomes_tool_error(client, store, alice, tool, arguments):
    await register(client, store, alice)
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


async def push_ref(store, name):
    repo = str(store.git_root / f"{PROJECT}{protocol.GIT_REPO_SUFFIX}")
    sha = await run(["git", "--git-dir", repo, "rev-parse", f"refs/heads/{BASE}"])
    await run(
        ["git", "--git-dir", repo, "update-ref", f"refs/heads/{name}", sha.stdout_text.strip()]
    )
    return sha.stdout_text.strip()


DONE = {
    protocol.PAYLOAD_EXIT_CODE: 1,
    protocol.PAYLOAD_DURATION_S: 3.0,
    protocol.PAYLOAD_SUMMARY: {
        protocol.SUMMARY_PASSED: 1,
        protocol.SUMMARY_FAILED: 1,
        protocol.SUMMARY_ERRORS: 0,
        protocol.SUMMARY_SKIPPED: 0,
    },
}


async def claim_test(store):
    return await store.queue.claim("pc", [], [protocol.JOB_KIND_TEST])


async def test_devlab_test_queues_without_waiting(client, store, alice):
    await register(client, store, alice)
    await push_ref(store, "test/alice/1")
    is_error, queued = await call(
        client, alice, protocol.TOOL_TEST, project=PROJECT, ref="test/alice/1", args=["-x"]
    )
    assert not is_error
    assert queued["result"] is None
    job = queued["job"]
    assert (job["kind"], job["status"]) == (protocol.JOB_KIND_TEST, protocol.STATUS_QUEUED)
    assert job["params"][protocol.FIELD_ARGS] == ["-x"]
    assert (await claim_test(store)).id == job["id"]


async def test_devlab_test_accepts_sha(client, store, alice):
    await register(client, store, alice)
    sha = await push_ref(store, "test/alice/1")
    is_error, queued = await call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=sha)
    assert not is_error and queued["job"]["params"][protocol.FIELD_REF] == sha


@pytest.mark.parametrize("ref", ["test/alice/nope", "0" * 40, "-x"])
async def test_devlab_test_missing_ref_hints_push(client, store, alice, ref):
    await register(client, store, alice)
    is_error, text = await call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=ref)
    assert is_error and "git push" in text and "test/alice/" in text
    assert await store.queue.list_jobs() == []


@pytest.mark.parametrize("wait", [-1, protocol.TEST_WAIT_MAX_S + 1])
async def test_devlab_test_wait_is_bounded(client, store, alice, wait):
    await register(client, store, alice)
    is_error, _ = await call(
        client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE, wait=wait
    )
    assert is_error


async def test_devlab_test_wait_returns_result_in_time(client, store, alice):
    await register(client, store, alice)
    waiting = asyncio.create_task(
        call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE, wait=30)
    )
    job = None
    while job is None:
        await asyncio.sleep(0.01)
        job = await claim_test(store)
    assert not waiting.done()
    await store.queue.post_from_executor(job.id, protocol.MSG_TEST_DONE, DONE, executor="pc")
    is_error, done = await asyncio.wait_for(waiting, 2)
    assert not is_error
    assert done["job"]["status"] == protocol.STATUS_DONE
    assert done["result"][protocol.PAYLOAD_EXIT_CODE] == 1


async def test_devlab_status_of_test_job_carries_result(client, store, alice):
    await register(client, store, alice)
    _, queued = await call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE)
    job_id = queued["job"]["id"]
    _, before = await call(client, alice, protocol.TOOL_STATUS, id=job_id)
    assert before["result"] is None
    job = await claim_test(store)
    await store.queue.post_from_executor(job.id, protocol.MSG_TEST_DONE, DONE, executor="pc")
    _, after = await call(client, alice, protocol.TOOL_STATUS, id=job_id)
    assert after["status"] == protocol.STATUS_DONE
    assert after["result"][protocol.PAYLOAD_EXIT_CODE] == 1
    assert after["result"][protocol.PAYLOAD_SUMMARY][protocol.SUMMARY_FAILED] == 1


async def test_devlab_status_of_agent_job_has_no_result(client, store, alice):
    await register(client, store, alice)
    _, submitted = await submit(client, alice)
    _, view = await call(client, alice, protocol.TOOL_STATUS, id=submitted["job"]["id"])
    assert "result" not in view


async def test_devlab_test_wait_failure_returns_reason(client, store, alice):
    await register(client, store, alice)
    waiting = asyncio.create_task(
        call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE, wait=30)
    )
    job = None
    while job is None:
        await asyncio.sleep(0.01)
        job = await claim_test(store)
    await store.queue.post_from_executor(
        job.id, protocol.MSG_FAILED, {protocol.PAYLOAD_TEXT: "нет образа"}, executor="pc"
    )
    _, failed = await asyncio.wait_for(waiting, 2)
    assert failed["job"]["status"] == protocol.STATUS_FAILED
    assert failed["result"][protocol.PAYLOAD_TEXT] == "нет образа"


async def test_devlab_test_wait_timeout_returns_id_and_result_goes_to_inbox(
    client, store, alice, monkeypatch
):
    await register(client, store, alice)
    monkeypatch.setattr(protocol, "TEST_WAIT_MAX_S", 1)
    started = asyncio.get_running_loop().time()
    is_error, queued = await call(
        client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE, wait=1
    )
    assert not is_error and queued["result"] is None
    assert queued["job"]["status"] == protocol.STATUS_QUEUED
    assert asyncio.get_running_loop().time() - started < 3
    job = await claim_test(store)
    await store.queue.post_from_executor(job.id, protocol.MSG_TEST_DONE, DONE, executor="pc")
    _, inbox = await call(client, alice, protocol.TOOL_INBOX)
    assert [m["kind"] for m in inbox["messages"]] == [protocol.MSG_TEST_DONE]


async def test_devlab_test_wait_does_not_block_other_requests(client, store, alice, bob):
    await register(client, store, alice)
    waiting = asyncio.create_task(
        call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE, wait=30)
    )
    await asyncio.sleep(0.1)
    assert not waiting.done()
    is_error, projects = await asyncio.wait_for(call(client, bob, protocol.TOOL_PROJECTS), 1)
    assert not is_error and projects == [PROJECT]
    assert (await asyncio.wait_for(client.get(protocol.HTTP_HEALTH_PATH), 1)).status == 200
    waiting.cancel()


async def test_devlab_test_log_returns_tail_and_ref(client, store, alice, bob, monkeypatch):
    await register(client, store, alice)
    _, queued = await call(client, alice, protocol.TOOL_TEST, project=PROJECT, ref=BASE)
    job_id = queued["job"]["id"]
    is_error, text = await call(client, alice, protocol.TOOL_TEST_LOG, id=job_id)
    assert is_error and "нет файла" in text
    directory = store.logs_root / job_id
    directory.mkdir(parents=True)
    (directory / protocol.LOG_FILE_OUTPUT).write_text("0123456789")
    (directory / protocol.LOG_FILE_JUNIT).write_text("<xml/>")
    _, full = await call(client, alice, protocol.TOOL_TEST_LOG, id=job_id)
    assert (full["text"], full["truncated"], full["bytes"], full["ref"]) == (
        "0123456789",
        False,
        10,
        BASE,
    )
    monkeypatch.setattr(protocol, "LOG_TAIL_MAX_BYTES", 4)
    _, tail = await call(client, alice, protocol.TOOL_TEST_LOG, id=job_id)
    assert (tail["text"], tail["truncated"]) == ("6789", True)
    _, junit = await call(
        client, alice, protocol.TOOL_TEST_LOG, id=job_id, file=protocol.LOG_FILE_JUNIT
    )
    assert junit["text"] == "ml/>"
    is_error, _ = await call(client, alice, protocol.TOOL_TEST_LOG, id=job_id, file="../x")
    assert is_error
    is_error, _ = await call(client, bob, protocol.TOOL_TEST_LOG, id=job_id)
    assert is_error


async def test_devlab_test_log_rejects_agent_job(client, store, alice):
    await register(client, store, alice)
    _, submitted = await submit(client, alice)
    is_error, _ = await call(client, alice, protocol.TOOL_TEST_LOG, id=submitted["job"]["id"])
    assert is_error
