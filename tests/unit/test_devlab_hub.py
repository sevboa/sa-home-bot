from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import http
from sa_home_bot.devlab.hub import service as hub_service
from sa_home_bot.devlab.hub.mcp_tools import TOOLS_BY_NAME
from sa_home_bot.devlab.hub.service import HubService, task_title
from sa_home_bot.devlab.hub.store import HubStore, hash_token
from sa_home_bot.proto.messages import ERR_BAD_REQUEST, ProtoError
from tests.unit.support.fakes import FakeNodes

GUARDED_PATH = "/guarded"


@http.roles(protocol.ROLE_EXECUTOR)
async def guarded(request: web.Request) -> web.Response:
    return web.json_response({"who": request[http.PRINCIPAL_KEY].name})


@pytest.fixture
async def store(tmp_path):
    hub_store = HubStore(tmp_path)
    await hub_store.open()
    yield hub_store
    await hub_store.close()


@pytest.fixture
async def client(store):
    app = http.build_app(store, FakeNodes())
    app.router.add_get(GUARDED_PATH, guarded)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_health_is_open(client):
    resp = await client.get(protocol.HTTP_HEALTH_PATH)
    assert resp.status == 200


@pytest.mark.parametrize("headers", [{}, bearer("dlh_wrong"), {"Authorization": "Basic abc"}])
async def test_bad_credentials_get_401(client, headers):
    resp = await client.get(GUARDED_PATH, headers=headers)
    assert resp.status == 401


async def test_revoked_token_gets_401(client, store):
    token = await store.issue_token("pc", protocol.ROLE_EXECUTOR)
    await store.revoke_token("pc")
    resp = await client.get(GUARDED_PATH, headers=bearer(token))
    assert resp.status == 401


@pytest.mark.parametrize(
    ("role", "status"),
    [
        (protocol.ROLE_EXECUTOR, 200),
        (protocol.ROLE_CLIENT, 403),
        (protocol.ROLE_GATEWAY, 403),
    ],
)
async def test_route_roles(client, store, role, status):
    token = await store.issue_token("pc", role)
    resp = await client.get(GUARDED_PATH, headers=bearer(token))
    assert resp.status == status
    if status == 200:
        assert (await resp.json())["who"] == "pc"


async def test_token_is_stored_only_as_hash(store, tmp_path):
    token = await store.issue_token("pc", protocol.ROLE_CLIENT)
    assert token.startswith(protocol.TOKEN_PREFIX)
    cur = await store._db.conn.execute("SELECT * FROM tokens")
    rows = [tuple(r) for r in await cur.fetchall()]
    assert hash_token(token) in rows[0]
    assert token not in rows[0]
    await store.close()
    for path in tmp_path.glob("hub.sqlite*"):
        assert token.encode() not in path.read_bytes()
    await store.open()


async def test_issue_and_revoke_through_service(store):
    service = HubService(store, FakeNodes())
    issued = await service.run_command(
        protocol.ACTION_ISSUE_TOKEN, {"name": "pc", "role": protocol.ROLE_CLIENT}
    )
    assert (await store.authenticate(issued["token"])).role == protocol.ROLE_CLIENT
    assert (await service.get_state())["active_tokens"] == 1
    await service.run_command(protocol.ACTION_REVOKE_TOKEN, {"name": "pc"})
    assert await store.authenticate(issued["token"]) is None
    reissued = await service.run_command(
        protocol.ACTION_ISSUE_TOKEN, {"name": "pc", "role": protocol.ROLE_GATEWAY}
    )
    assert reissued["token"] != issued["token"]


@pytest.mark.parametrize(
    ("action", "args"),
    [
        (protocol.ACTION_ISSUE_TOKEN, {"name": "pc", "role": "root"}),
        (protocol.ACTION_ISSUE_TOKEN, {"name": "", "role": protocol.ROLE_CLIENT}),
        (protocol.ACTION_REVOKE_TOKEN, {"name": "nobody"}),
    ],
)
async def test_service_rejects_bad_requests(store, action, args):
    with pytest.raises(ProtoError):
        await HubService(store, FakeNodes()).run_command(action, args)


async def test_duplicate_active_name_rejected(store):
    service = HubService(store, FakeNodes())
    args = {"name": "pc", "role": protocol.ROLE_CLIENT}
    await service.run_command(protocol.ACTION_ISSUE_TOKEN, args)
    with pytest.raises(ProtoError):
        await service.run_command(protocol.ACTION_ISSUE_TOKEN, args)


def test_token_actions_are_callable_by_nodectl(tmp_path):
    description = HubService(HubStore(tmp_path), FakeNodes()).describe()
    issue = description.find_action(protocol.ACTION_ISSUE_TOKEN)
    revoke = description.find_action(protocol.ACTION_REVOKE_TOKEN)
    assert issue is not None and [p.name for p in issue.params] == ["name", "role"]
    assert revoke is not None and [p.name for p in revoke.params] == ["name", "role"]


OWNER_ACTION_IDS = (
    protocol.ACTION_OWNER_INBOX,
    protocol.ACTION_OWNER_JOB,
    protocol.ACTION_OWNER_POST,
)
CLIENT_A = "claude-a"
CLIENT_B = "claude-b"
MODEL = "qwen-local"


@pytest.fixture
async def owner(store):
    queue = store.queue
    await queue.add_project("demo")
    service = HubService(store, FakeNodes())
    jobs = {}
    for client in (CLIENT_A, CLIENT_B):
        job = await queue.submit(client, "demo", MODEL, "# Заголовок\nтело")
        await queue.claim("mycraft", [MODEL])
        jobs[client] = job
    return service, queue, jobs


def test_owner_actions_are_declared(tmp_path):
    description = HubService(HubStore(tmp_path), FakeNodes()).describe()
    for action_id in OWNER_ACTION_IDS:
        assert description.find_action(action_id) is not None


def test_owner_actions_have_no_mcp_tool():
    assert not set(TOOLS_BY_NAME) & set(OWNER_ACTION_IDS)


@pytest.mark.parametrize(
    ("task", "title"),
    [("# Цель\nтекст", "Цель"), ("\n\n## Второй\n", "Второй"), ("", ""), ("x" * 200, "x" * 120)],
)
def test_task_title(task, title):
    assert task_title(task) == title


async def test_owner_inbox_sees_all_clients_without_reading(owner):
    service, queue, jobs = owner
    for job in jobs.values():
        await queue.post_from_executor(
            job.id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: "как?"}
        )
    result = await service.run_command(protocol.ACTION_OWNER_INBOX, {"since": 0})
    assert {m["job"]["client"] for m in result["messages"]} == {CLIENT_A, CLIENT_B}
    first = result["messages"][0]
    assert first["job"]["title"] == "Заголовок"
    assert "task" not in first["job"]
    assert first["job"]["status"] == protocol.STATUS_WAITING_OWNER
    assert result["cursor"] == result["messages"][-1]["seq"]
    assert not any(m.read for m, _ in await queue.inbox_all())
    again = await service.run_command(protocol.ACTION_OWNER_INBOX, {"since": result["cursor"]})
    assert again == {"messages": [], "cursor": result["cursor"]}


async def test_owner_inbox_negative_since_returns_current_cursor(owner):
    service, queue, jobs = owner
    await queue.post_from_executor(jobs[CLIENT_A].id, protocol.MSG_STARTED)
    result = await service.run_command(protocol.ACTION_OWNER_INBOX, {"since": -1})
    assert result == {"messages": [], "cursor": await queue.max_seq()}


async def test_owner_inbox_limit(owner):
    service, queue, jobs = owner
    for _ in range(3):
        await queue.post_from_executor(jobs[CLIENT_A].id, protocol.MSG_STEP_DONE)
    result = await service.run_command(protocol.ACTION_OWNER_INBOX, {"since": 0, "limit": 2})
    assert len(result["messages"]) == 2
    with pytest.raises(ProtoError):
        await service.run_command(protocol.ACTION_OWNER_INBOX, {"limit": 0})


async def test_owner_job_has_title_and_last_question(owner):
    service, queue, jobs = owner
    job = jobs[CLIENT_A]
    await queue.post_from_executor(
        job.id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: "первый?"}
    )
    await queue.post_from_client(
        CLIENT_A, job.id, protocol.MSG_ANSWER, {protocol.PAYLOAD_TEXT: "да"}
    )
    await queue.post_from_executor(
        job.id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: "второй?"}
    )
    view = await service.run_command(protocol.ACTION_OWNER_JOB, {"id": job.id})
    assert view["question"] == "второй?"
    assert view["title"] == "Заголовок"
    assert view["status"] == protocol.STATUS_WAITING_OWNER
    assert "task" not in view
    with pytest.raises(ProtoError):
        await service.run_command(protocol.ACTION_OWNER_JOB, {"id": "nope"})


async def test_owner_post_answers_for_the_job_client(owner):
    service, queue, jobs = owner
    job = jobs[CLIENT_B]
    await queue.post_from_executor(job.id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: "?"})
    result = await service.run_command(
        protocol.ACTION_OWNER_POST, {"id": job.id, "kind": protocol.MSG_ANSWER, "text": "делай"}
    )
    assert result["status"] == protocol.STATUS_RUNNING
    sent = await queue.poll(job_id=job.id)
    assert sent[-1].seq == result["seq"]
    assert sent[-1].payload == {
        protocol.PAYLOAD_VIA: protocol.VIA_TELEGRAM,
        protocol.PAYLOAD_TEXT: "делай",
    }


async def test_owner_post_resume_with_model_and_stop(owner):
    service, queue, jobs = owner
    job = jobs[CLIENT_A]
    await queue.post_from_executor(job.id, protocol.MSG_FAILED, {})
    resumed = await service.run_command(
        protocol.ACTION_OWNER_POST, {"id": job.id, "kind": protocol.MSG_RESUME, "model": "other"}
    )
    assert resumed["status"] == protocol.STATUS_QUEUED
    assert (await queue.get_job(job.id)).model == "other"
    stopped = await service.run_command(
        protocol.ACTION_OWNER_POST, {"id": job.id, "kind": protocol.MSG_STOP}
    )
    assert stopped["status"] == protocol.STATUS_STOPPED


@pytest.mark.parametrize(
    ("args", "prepare"),
    [
        ({"kind": protocol.MSG_ANSWER, "text": "  "}, True),
        ({"kind": protocol.MSG_ANSWER, "text": "x"}, False),
        ({"kind": protocol.MSG_STARTED}, True),
        ({"kind": protocol.MSG_RESUME, "model": ""}, True),
    ],
)
async def test_owner_post_rejects_bad_requests(owner, args, prepare):
    service, queue, jobs = owner
    job = jobs[CLIENT_A]
    if prepare:
        await queue.post_from_executor(job.id, protocol.MSG_QUESTION, {protocol.PAYLOAD_TEXT: "?"})
    with pytest.raises(ProtoError) as caught:
        await service.run_command(protocol.ACTION_OWNER_POST, {"id": job.id, **args})
    assert caught.value.code == ERR_BAD_REQUEST
    assert (await queue.get_job(job.id)).status in (
        protocol.STATUS_WAITING_OWNER,
        protocol.STATUS_RUNNING,
    )


async def test_owner_inbox_limit_is_capped(owner, monkeypatch):
    service, queue, jobs = owner
    monkeypatch.setattr(hub_service, "OWNER_INBOX_MAX_LIMIT", 2)
    for _ in range(3):
        await queue.post_from_executor(jobs[CLIENT_A].id, protocol.MSG_STEP_DONE)
    result = await service.run_command(protocol.ACTION_OWNER_INBOX, {"since": 0, "limit": 1000})
    assert len(result["messages"]) == 2
