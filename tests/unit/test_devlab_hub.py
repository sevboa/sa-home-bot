from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sa_home_bot.devlab import protocol
from sa_home_bot.devlab.hub import http
from sa_home_bot.devlab.hub.service import HubService
from sa_home_bot.devlab.hub.store import HubStore, hash_token
from sa_home_bot.proto.messages import ProtoError
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
    assert revoke is not None and [p.name for p in revoke.params] == ["name"]
